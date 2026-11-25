"""Servers the UI manages: this machine plus remote Docker hosts reached over SSH.

The active server is a context variable, set per request from a cookie by ``ServerMiddleware``.
Background jobs inherit it (asyncio tasks copy the context), so a job keeps talking to the server it
was started on. ``docker.py`` adds ``current().env()`` to every docker invocation, which points the
docker CLI at ``ssh://fdm-<name>`` through an SSH wrapper using the UI's own ssh config.
"""
from __future__ import annotations

import asyncio
import contextvars
import io
import json
import os
import re
import shlex
import shutil
import tarfile
from dataclasses import asdict, dataclass
from http.cookies import SimpleCookie
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

from . import config

LOCAL = "local"
COOKIE = "fdm_server"
SERVERS_FILE = config.DATA_DIR / "servers.json"
SSH_DIR = config.DATA_DIR / "ssh"
SSH_CONFIG = SSH_DIR / "config"
SSH_WRAPPER_DIR = SSH_DIR / "bin"
APP_KEY = SSH_DIR / "id_ed25519"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-:]{0,252}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
SAFE_PATH_RE = re.compile(r"^/[\w.\-/]+$")


@dataclass(frozen=True)
class Server:
    name: str
    host: str = ""
    user: str = ""
    port: int = 22
    key: str = ""
    remote_home: str = ""

    @property
    def local(self) -> bool:
        return self.name == LOCAL

    @property
    def alias(self) -> str:
        return f"fdm-{self.name}"

    @property
    def label(self) -> str:
        return "This machine" if self.local else f"{self.user}@{self.host}" + ("" if self.port == 22 else f":{self.port}")

    @property
    def data_dir(self) -> Path:
        """Where the UI keeps this server's compose files and state, on this machine."""
        return config.DATA_DIR if self.local else config.DATA_DIR / "servers" / self.name

    @property
    def remote_root(self) -> str:
        """The same data folder on the server's own filesystem (bind-mount sources must exist there)."""
        return str(config.DATA_DIR) if self.local else f"{self.remote_home}/frappe-docker-manager"

    def env(self) -> dict:
        if self.local:
            return {}
        return {"DOCKER_HOST": f"ssh://{self.alias}",
                "PATH": f"{SSH_WRAPPER_DIR}{os.pathsep}{os.environ.get('PATH', '')}"}


LOCAL_SERVER = Server(LOCAL)
_current: contextvars.ContextVar[Server | None] = contextvars.ContextVar("fdm_server", default=None)


def current() -> Server:
    return _current.get() or LOCAL_SERVER


def use(name: str) -> None:
    _current.set(get(name) or LOCAL_SERVER)


def load_all() -> list[Server]:
    try:
        rows = json.loads(SERVERS_FILE.read_text())
    except (OSError, ValueError):
        rows = []
    out = [LOCAL_SERVER]
    for r in rows:
        try:
            out.append(Server(**r))
        except TypeError:
            continue
    return out


def get(name: str) -> Server | None:
    return next((s for s in load_all() if s.name == name), None)


def _save(remotes: list[Server]) -> None:
    SERVERS_FILE.write_text(json.dumps([asdict(s) for s in remotes], indent=2))
    write_ssh_files(remotes)


def validate(form: dict) -> Server:
    name = str(form.get("name", "")).strip().lower()
    host = str(form.get("host", "")).strip()
    user = str(form.get("user", "")).strip()
    key = os.path.expanduser(str(form.get("key", "")).strip())
    try:
        port = int(form.get("port") or 22)
    except ValueError as exc:
        raise ValueError("Port must be a number") from exc
    if not NAME_RE.match(name) or name == LOCAL:
        raise ValueError("Name: lowercase letters, digits and '-', up to 31 characters (not 'local')")
    if not HOST_RE.match(host):
        raise ValueError("Host must be a hostname or IP address")
    if not USER_RE.match(user):
        raise ValueError("Invalid SSH user name")
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    if not key or any(c in key for c in "\"\n\r") or not Path(key).is_file():
        raise ValueError(f"SSH private key not found: {key or '(empty)'}")
    if get(name):
        raise ValueError(f"A server named '{name}' already exists")
    return Server(name=name, host=host, user=user, port=port, key=key)


def _control_dir() -> Path:
    """Short private folder for SSH multiplexing sockets (socket paths must stay under ~104 bytes)."""
    d = Path("/tmp") / f"fdm-{os.getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    st = d.lstat()
    if d.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(f"{d} is not a private folder owned by you - remove it and retry")
    return d


def write_ssh_files(remotes: list[Server] | None = None) -> None:
    remotes = [s for s in (remotes if remotes is not None else load_all()) if not s.local]
    SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    blocks = []
    for s in remotes:
        blocks.append("\n".join([
            f"Host {s.alias}",
            f"  HostName {s.host}",
            f"  User {s.user}",
            f"  Port {s.port}",
            f'  IdentityFile "{s.key}"',
            "  IdentitiesOnly yes",
            "  BatchMode yes",
            "  LogLevel ERROR",
            "  StrictHostKeyChecking accept-new",
            f'  UserKnownHostsFile "{SSH_DIR / "known_hosts"}"',
            "  ConnectTimeout 10",
            "  ServerAliveInterval 30",
            # Every docker CLI call opens an SSH session; multiplexing makes them cheap.
            "  ControlMaster auto",
            f'  ControlPath "{_control_dir() / "%C"}"',
            "  ControlPersist 10m",
        ]))
    SSH_CONFIG.write_text("\n\n".join(blocks) + "\n")
    SSH_CONFIG.chmod(0o600)
    SSH_WRAPPER_DIR.mkdir(mode=0o700, exist_ok=True)
    real = shutil.which("ssh") or "/usr/bin/ssh"
    wrapper = SSH_WRAPPER_DIR / "ssh"
    wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(real)} -F {shlex.quote(str(SSH_CONFIG))} \"$@\"\n")
    wrapper.chmod(0o700)


async def _run(args: list[str], stdin: bytes | None = None, timeout: float = 60) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError as exc:
        return 127, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"Timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def ssh(server: Server, command: str, stdin: bytes | None = None, timeout: float = 60) -> tuple[int, str]:
    """Run a shell command on the server (callers quote every value with shlex.quote)."""
    return await _run([str(SSH_WRAPPER_DIR / "ssh"), server.alias, command], stdin=stdin, timeout=timeout)


async def connect(server: Server) -> Server:
    """Check SSH and Docker access and learn the remote home folder. Raises ValueError with a readable reason."""
    write_ssh_files([*[s for s in load_all() if not s.local and s.name != server.name], server])
    code, out = await ssh(server, 'printf %s "$HOME"', timeout=30)
    if code != 0:
        raise ValueError(f"SSH failed: {out or 'no output'}. Is the public key in ~/.ssh/authorized_keys on the server?")
    home = out.strip().splitlines()[-1] if out.strip() else ""
    if not SAFE_PATH_RE.match(home):
        raise ValueError(f"Unexpected remote home folder: {home!r}")
    code, out = await ssh(server, "docker version --format '{{.Server.Version}}'", timeout=30)
    if code != 0:
        raise ValueError(f"Connected over SSH, but Docker failed: {out}. Is Docker installed, and is "
                         f"'{server.user}' in the docker group (sudo usermod -aG docker {server.user})?")
    code, out2 = await ssh(server, "docker compose version --short", timeout=30)
    if code != 0:
        raise ValueError("Docker works, but the compose plugin is missing on the server (install docker-compose-plugin)")
    return Server(**{**asdict(server), "remote_home": home})


def add(server: Server) -> None:
    _save([*[s for s in load_all() if not s.local and s.name != server.name], server])


def remove(name: str) -> None:
    _save([s for s in load_all() if not s.local and s.name != name])


async def status(server: Server) -> dict:
    """Docker version and container counts on one server, without changing the current server."""
    from . import docker  # noqa: PLC0415 - avoid a module import cycle

    token = _current.set(server)
    try:
        r = await docker.docker("info", "--format", "{{.ServerVersion}}\t{{.Containers}}\t{{.ContainersRunning}}",
                                timeout=25)
    finally:
        _current.reset(token)
    if not r.ok:
        return {"ok": False, "error": (r.text or "unreachable").splitlines()[-1][:300]}
    parts = r.out.strip().split("\t")
    return {"ok": True, "version": parts[0], "containers": parts[1] if len(parts) > 1 else "?",
            "running": parts[2] if len(parts) > 2 else "?"}


async def upload_dir(server: Server, local_dir: Path, remote_dir: str) -> None:
    """Copy a local folder's contents to ``remote_dir`` on the server (replacing files with the same name)."""
    if not SAFE_PATH_RE.match(remote_dir) or not remote_dir.startswith(server.remote_root + "/"):
        raise ValueError(f"Refusing to write outside {server.remote_root}: {remote_dir}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(local_dir), arcname=".")
    q = shlex.quote(remote_dir)
    code, out = await ssh(server, f"mkdir -p {q} && tar xf - -C {q}", stdin=buf.getvalue(), timeout=120)
    if code != 0:
        raise RuntimeError(f"Copying files to {server.name}:{remote_dir} failed: {out}")


async def make_remote_dir(server: Server, remote_dir: str) -> None:
    if not SAFE_PATH_RE.match(remote_dir) or not remote_dir.startswith(server.remote_root + "/"):
        raise ValueError(f"Refusing to write outside {server.remote_root}: {remote_dir}")
    code, out = await ssh(server, f"mkdir -p {shlex.quote(remote_dir)}", timeout=60)
    if code != 0:
        raise RuntimeError(f"Could not create {server.name}:{remote_dir}: {out}")


async def remove_remote_dir(server: Server, remote_dir: str) -> None:
    if not SAFE_PATH_RE.match(remote_dir) or not remote_dir.startswith(server.remote_root + "/sites/"):
        raise ValueError(f"Refusing to delete {remote_dir}")
    await ssh(server, f"rm -rf {shlex.quote(remote_dir)}", timeout=120)


async def generate_key() -> str:
    """Create the UI's own SSH key pair once and return the public key."""
    SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not APP_KEY.exists():
        code, out = await _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "frappe-docker-manager",
                                "-f", str(APP_KEY)], timeout=30)
        if code != 0:
            raise RuntimeError(f"ssh-keygen failed: {out}")
    return APP_KEY.with_suffix(".pub").read_text().strip()


def app_public_key() -> str:
    try:
        return APP_KEY.with_suffix(".pub").read_text().strip()
    except OSError:
        return ""


def default_key() -> str:
    for p in (APP_KEY, Path.home() / ".ssh" / "id_ed25519", Path.home() / ".ssh" / "id_rsa"):
        if p.is_file():
            return str(p)
    return ""


class ServerMiddleware:
    """Selects the server for each request from the ``fdm_server`` cookie (default: this machine)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            raw = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"cookie"), "")
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except Exception:  # noqa: BLE001 - malformed cookies just mean "default server"
                cookie = SimpleCookie()
            name = cookie[COOKIE].value if COOKIE in cookie else LOCAL
            token = _current.set(get(name) or LOCAL_SERVER)
            try:
                return await self.app(scope, receive, send)
            finally:
                _current.reset(token)
        await self.app(scope, receive, send)
