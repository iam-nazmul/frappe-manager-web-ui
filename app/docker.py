"""Thin async wrappers around the docker CLI (no shell is ever involved)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Sequence

from . import servers

LineCallback = Callable[[str], Awaitable[None] | None]


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return (self.out + ("\n" + self.err if self.err else "")).strip()


def docker_bin() -> str:
    return shutil.which("docker") or "docker"


def proc_env(extra: dict | None = None) -> dict:
    """Environment for docker subprocesses: points the CLI at the selected server (see servers.py)."""
    return {**os.environ, **servers.current().env(), **(extra or {})}


async def run(args: Sequence[str], *, timeout: float | None = 120, stdin: bytes | None = None,
              env: dict | None = None) -> Result:
    """Run a command and capture its output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env(env),
        )
    except FileNotFoundError as exc:
        return Result(127, "", str(exc))
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return Result(124, "", f"Timed out after {timeout}s: {' '.join(args[:4])}")
    return Result(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


async def docker(*args: str, timeout: float | None = 120, stdin: bytes | None = None) -> Result:
    return await run([docker_bin(), *args], timeout=timeout, stdin=stdin)


async def stream(args: Sequence[str], on_line: LineCallback, *, cwd: str | None = None,
                 env: dict | None = None) -> int:
    """Run a command, calling ``on_line`` for every output line (stdout+stderr merged)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=proc_env(env),
        )
    except FileNotFoundError as exc:
        r = on_line(f"ERROR: {exc}")
        if asyncio.iscoroutine(r):
            await r
        return 127
    assert proc.stdout
    buf = b""
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            # docker pull / compose use \r for progress; treat it as a line break
            *lines, buf = buf.replace(b"\r", b"\n").split(b"\n")
            for raw in lines:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    r = on_line(line)
                    if asyncio.iscoroutine(r):
                        await r
        if buf.strip():
            r = on_line(buf.decode(errors="replace").rstrip())
            if asyncio.iscoroutine(r):
                await r
    except asyncio.CancelledError:
        proc.kill()
        raise
    return await proc.wait()


async def compose_cmd() -> list[str]:
    """Prefer the compose v2 plugin, fall back to docker-compose v1."""
    if (await docker("compose", "version", timeout=15)).ok:
        return [docker_bin(), "compose"]
    legacy = shutil.which("docker-compose")
    if legacy:
        return [legacy]
    raise RuntimeError("Neither 'docker compose' nor 'docker-compose' is available")


async def inspect(names: Iterable[str], kind: str = "container") -> list[dict]:
    names = list(names)
    if not names:
        return []
    r = await docker(kind, "inspect", *names, timeout=30)
    if not r.out.strip():
        return []
    try:
        return json.loads(r.out)
    except json.JSONDecodeError:
        return []


async def all_containers() -> list[dict]:
    r = await docker("ps", "-aq", "--no-trunc", timeout=30)
    ids = r.out.split()
    return await inspect(ids)


async def info() -> dict:
    r = await docker("info", "--format", "{{json .}}", timeout=20)
    if not r.ok:
        return {"ok": False, "error": r.text or "Docker is not reachable"}
    try:
        data = json.loads(r.out)
    except json.JSONDecodeError:
        return {"ok": False, "error": r.text}
    if data.get("ServerErrors"):
        return {"ok": False, "error": "; ".join(data["ServerErrors"])}
    comp = await docker("compose", "version", "--short", timeout=15)
    return {
        "ok": True,
        "server_version": data.get("ServerVersion"),
        "os": data.get("OperatingSystem"),
        "cpus": data.get("NCPU"),
        "mem_gb": round((data.get("MemTotal") or 0) / 1024 ** 3, 1),
        "containers_running": data.get("ContainersRunning"),
        "containers": data.get("Containers"),
        "images": data.get("Images"),
        "compose_version": comp.out.strip() if comp.ok else None,
    }


async def network_exists(name: str) -> bool:
    return (await docker("network", "inspect", name, timeout=15)).ok


async def ensure_network(name: str) -> bool:
    if await network_exists(name):
        return False
    r = await docker("network", "create", "--driver", "bridge", name, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Could not create network {name}: {r.text}")
    return True


def container_summary(c: dict) -> dict:
    state = c.get("State", {})
    labels = c.get("Config", {}).get("Labels") or {}
    health = (state.get("Health") or {}).get("Status")
    return {
        "id": c.get("Id", "")[:12],
        "name": c.get("Name", "").lstrip("/"),
        "service": labels.get("com.docker.compose.service", ""),
        "image": c.get("Config", {}).get("Image", ""),
        "state": state.get("Status", "unknown"),
        "running": bool(state.get("Running")),
        "exit_code": state.get("ExitCode"),
        "health": health,
        "started": state.get("StartedAt", "")[:19].replace("T", " "),
        "restart_policy": (c.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name", ""),
        "networks": sorted((c.get("NetworkSettings", {}).get("Networks") or {}).keys()),
    }
