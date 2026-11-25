"""Frappe/ERPNext Docker Manager - web UI (FastAPI + Jinja2)."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, docker, jobs, sites, terminal, traefik, tunnel, versions
from .security import GuardMiddleware

app = FastAPI(title="Frappe Docker Manager", docs_url="/api/docs", redoc_url=None)
app.add_middleware(GuardMiddleware)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
templates.env.globals["config"] = config


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"nav": name.split(".")[0], **ctx})


async def require_site(project: str) -> dict:
    site = await sites.get(project)
    if not site:
        raise HTTPException(404, f"Site '{project}' not found")
    return site


def job_response(job: jobs.Job) -> JSONResponse:
    return JSONResponse({"job_id": job.id, "title": job.title})


def bad(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


# --------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    info, tst, all_sites = await asyncio.gather(docker.info(), traefik.status(), sites.discover())
    for s in all_sites:
        s.pop("env", None)  # never render passwords into the page
    return page(request, "dashboard.html", info=info, traefik=tst, sites=all_sites, jobs=jobs.recent(8))


@app.get("/traefik", response_class=HTMLResponse)
async def traefik_page(request: Request):
    tst = await traefik.status()
    suggested = traefik.suggest_port(8081, 8099, {tst["config"]["dashboard_port"]})
    return page(request, "traefik.html", traefik=tst, suggested_port=suggested,
                compose=traefik.compose_text(), has_cf_token="CF_DNS_API_TOKEN=" in traefik._read_env())


@app.get("/tunnel", response_class=HTMLResponse)
async def tunnel_page(request: Request):
    all_sites = await sites.discover()
    public = [{"domain": s["domain"], "project": s["project"]} for s in all_sites
              if not s["domain"].endswith((".localhost", ".local", ".test"))]
    return page(request, "tunnel.html", tunnel=await tunnel.status(), public_sites=public,
                config_text=tunnel.config_text())


@app.get("/sites/new", response_class=HTMLResponse)
async def new_site_page(request: Request):
    tst = await traefik.status()
    return page(request, "site_new.html", traefik=tst, dev_root=str(config.DEV_APPS_ROOT))


@app.get("/sites/{project}", response_class=HTMLResponse)
async def site_page(request: Request, project: str):
    site = await require_site(project)
    return page(request, "site_detail.html", site=site, hosts=sites.hosts_status(site["domain"]),
                tunnel=tunnel.load_state(), tunnel_ready=tunnel.cert_path().exists(),
                bench_commands=sites.BENCH_COMMANDS, log_sources=list(sites.LOG_SOURCES),
                jobs=jobs.recent(10, project), running_job=jobs.running_for(project))


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    return page(request, "jobs.html", jobs=jobs.recent(100))


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found (jobs are kept in memory until the UI restarts)")
    return page(request, "job.html", job=job)


@app.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request):
    df = await docker.docker("system", "df", timeout=60)
    nets = await docker.docker("network", "ls", "--format", "{{.Name}}\t{{.Driver}}\t{{.Scope}}")
    return page(request, "tools.html", df=df.text, networks=[n.split("\t") for n in nets.out.splitlines()],
                logs=sorted(config.JOBS_DIR.glob("*.log"), reverse=True)[:30])


# --------------------------------------------------------------------------- status APIs

@app.get("/api/status")
async def api_status():
    info, tst, all_sites = await asyncio.gather(docker.info(), traefik.status(), sites.discover())
    for s in all_sites:
        s.pop("env", None)
    return {"docker": info, "traefik": tst, "sites": all_sites}


@app.get("/api/versions")
async def api_versions(refresh: bool = False):
    return await versions.list_versions(force=refresh)


@app.get("/api/ports/suggest")
async def api_suggest_port(start: int = 8081):
    return {"port": traefik.suggest_port(max(1, start), min(65535, start + 30))}


# --------------------------------------------------------------------------- jobs

@app.get("/api/jobs")
async def api_jobs(scope: str | None = None):
    return [j.as_dict() for j in jobs.recent(50, scope)]


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return {**job.as_dict(), "lines": job.lines}


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    async def gen():
        seen = 0
        yield f"event: meta\ndata: {json.dumps(job.as_dict())}\n\n"
        while True:
            if await request.is_disconnected():
                return
            new = job.lines[seen:]
            seen += len(new)
            for line in new:
                yield f"data: {json.dumps(line)}\n\n"
            if job.status != "running" and seen >= len(job.lines):
                yield f"event: done\ndata: {json.dumps(job.as_dict())}\n\n"
                return
            await job.wait_new(seen)
            if not new:
                yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- traefik

@app.post("/api/traefik/setup")
async def api_traefik_setup(request: Request):
    try:
        cfg = traefik.validate(await request.json())
    except ValueError as exc:
        return bad(str(exc))
    return job_response(jobs.start("Set up Traefik", "traefik", lambda j: traefik.setup(j, cfg)))


@app.post("/api/traefik/fix-network")
async def api_fix_network():
    return job_response(jobs.start("Repair traefik_proxy network", "traefik", traefik.fix_network))


@app.post("/api/traefik/{action}")
async def api_traefik_action(action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(404)
    r = await traefik.control(action)
    return {"ok": r.ok, "output": r.text}


@app.get("/api/traefik/logs")
async def api_traefik_logs(lines: int = 200):
    r = await docker.docker("logs", "--tail", str(max(10, min(lines, 5000))), config.TRAEFIK_CONTAINER)
    return PlainTextResponse(r.text)


# --------------------------------------------------------------------------- cloudflare tunnel

@app.get("/api/tunnel/tunnels")
async def api_tunnel_list():
    try:
        return await tunnel.list_tunnels()
    except (RuntimeError, ValueError) as exc:
        return bad(str(exc), 502)


@app.post("/api/tunnel/login")
async def api_tunnel_login():
    return job_response(jobs.start("Cloudflare login", "tunnel", tunnel.login))


@app.post("/api/tunnel/use")
async def api_tunnel_use(request: Request):
    name = str((await request.json()).get("name", "")).strip()
    if not tunnel.NAME_RE.match(name):
        return bad("Tunnel name: letters, digits, '-', '_' or '.', up to 63 characters")
    return job_response(jobs.start(f"Use tunnel {name}", "tunnel", lambda j: tunnel.use_tunnel(j, name)))


@app.post("/api/tunnel/apply")
async def api_tunnel_apply():
    return job_response(jobs.start("Restart tunnel connector", "tunnel", tunnel.apply))


@app.post("/api/tunnel/publish")
async def api_tunnel_publish(request: Request):
    site = await require_site(str((await request.json()).get("project", "")))
    return job_response(jobs.start(f"Publish {site['domain']}", "tunnel", lambda j: tunnel.publish(j, site)))


@app.post("/api/tunnel/unpublish")
async def api_tunnel_unpublish(request: Request):
    hostname = str((await request.json()).get("hostname", ""))
    if hostname not in tunnel.public_hostnames():
        return bad(f"{hostname} is not published")
    site = next((s for s in await sites.discover() if s["domain"] == hostname), None)
    return job_response(jobs.start(f"Unpublish {hostname}", "tunnel", lambda j: tunnel.unpublish(j, hostname, site)))


@app.get("/api/tunnel/logs")
async def api_tunnel_logs(lines: int = 200):
    r = await docker.docker("logs", "--tail", str(max(10, min(lines, 5000))), config.TUNNEL_CONTAINER)
    return PlainTextResponse(r.text)


@app.post("/api/tunnel/{action}")
async def api_tunnel_action(action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(404)
    r = await tunnel.control(action)
    return {"ok": r.ok, "output": r.text}


# --------------------------------------------------------------------------- sites

@app.post("/api/sites")
async def api_create_site(request: Request):
    try:
        opts = sites.validate_new(await request.json())
    except ValueError as exc:
        return bad(str(exc))
    if await sites.get(opts["project"]):
        return bad(f"A site named {opts['domain']} already exists")
    job = jobs.start(f"Create site {opts['domain']}", opts["project"], lambda j: sites.create(j, opts))
    return job_response(job)


@app.get("/api/sites/{project}")
async def api_site(project: str):
    site = await require_site(project)
    site.pop("env", None)
    running = jobs.running_for(project)
    return {**site, "running_job": running.as_dict() if running else None}


@app.get("/api/sites/{project}/credentials")
async def api_site_credentials(project: str):
    site = await require_site(project)
    env = site["env"]
    return {"admin_user": "Administrator", "admin_password": env.get("ADMIN_PASSWORD", "admin (default)"),
            "db_root_password": env.get("DB_PASSWORD", "(unknown)"),
            "redis_password": env.get("REDIS_PASSWORD", "(unknown)")}


@app.get("/api/sites/{project}/compose")
async def api_site_compose(project: str):
    site = await require_site(project)
    try:
        return PlainTextResponse(Path(site["compose_file"]).read_text())
    except OSError:
        return PlainTextResponse("(compose file not found)")


@app.post("/api/sites/{project}/containers/{action}")
async def api_site_containers(project: str, action: str):
    if action not in ("start", "stop", "restart", "recreate", "down"):
        raise HTTPException(404)
    site = await require_site(project)
    return job_response(jobs.start(f"{action.title()} {site['domain']}", project,
                                   lambda j: sites.container_action(site, action, j)))


@app.post("/api/sites/{project}/container/{name}/{action}")
async def api_single_container(project: str, name: str, action: str):
    site = await require_site(project)
    if action not in ("start", "stop", "restart") or name not in {c["name"] for c in site["containers"]}:
        raise HTTPException(400, "Invalid container action")
    r = await docker.docker(action, name, timeout=120)
    return {"ok": r.ok, "output": r.text}


@app.post("/api/sites/{project}/delete")
async def api_site_delete(request: Request, project: str):
    site = await require_site(project)
    body = await request.json()
    if body.get("confirm") != site["domain"]:
        return bad("Type the site name exactly to confirm deletion")
    remove_dev = bool(body.get("remove_dev_dir"))
    return job_response(jobs.start(f"Delete {site['domain']}", project,
                                   lambda j: sites.delete(site, j, remove_dev)))


@app.get("/api/sites/{project}/processes")
async def api_processes(project: str):
    return await sites.supervisor_status(await require_site(project))


@app.post("/api/sites/{project}/processes")
async def api_process_action(request: Request, project: str):
    site = await require_site(project)
    body = await request.json()
    try:
        r = await sites.supervisor_action(site, body.get("action", ""), body.get("process", ""))
    except ValueError as exc:
        return bad(str(exc))
    return {"ok": r.ok, "output": r.text}


@app.get("/api/sites/{project}/logs")
async def api_logs(project: str, source: str = "web", lines: int = 300):
    site = await require_site(project)
    try:
        cmd = sites.log_command(site, source, follow=False, lines=lines)
    except ValueError as exc:
        return bad(str(exc))
    r = await docker.run(cmd, timeout=30)
    return PlainTextResponse(r.text or "(empty)")


@app.get("/api/sites/{project}/logs/stream")
async def api_logs_stream(request: Request, project: str, source: str = "web", lines: int = 200):
    site = await require_site(project)
    try:
        cmd = sites.log_command(site, source, follow=True, lines=lines)
    except ValueError as exc:
        return bad(str(exc))

    async def gen():
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        assert proc.stdout
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), 15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keep-alive\n\n"
                    continue
                if not line:
                    yield f"event: done\ndata: {json.dumps('[log stream ended]')}\n\n"
                    break
                yield f"data: {json.dumps(line.decode(errors='replace').rstrip())}\n\n"
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/sites/{project}/bench")
async def api_bench(request: Request, project: str):
    site = await require_site(project)
    body = await request.json()
    command = body.get("command", "")
    params = body.get("params") or {}
    title = f"bench {command}" + (f" {params.get('source') or params.get('app') or ''}" if params else "")
    return job_response(jobs.start(title.strip(), project, lambda j: sites.bench(site, j, command, params)))


@app.get("/api/sites/{project}/apps")
async def api_apps(project: str):
    site = await require_site(project)
    try:
        return await sites.list_apps(site)
    except RuntimeError as exc:
        return bad(str(exc), 502)


@app.get("/api/sites/{project}/backups")
async def api_backups(project: str):
    return await sites.list_backups(await require_site(project))


@app.post("/api/sites/{project}/fix-db")
async def api_fix_db(project: str):
    site = await require_site(project)
    return job_response(jobs.start("Fix DB permissions", project, lambda j: sites.fix_db_permissions(site, j)))


@app.post("/api/sites/{project}/restart-policy")
async def api_restart_policy(project: str):
    site = await require_site(project)
    return job_response(jobs.start("Fix restart policies", project, lambda j: sites.fix_restart_policy(site, j)))


@app.post("/api/sites/{project}/packages")
async def api_packages(request: Request, project: str):
    site = await require_site(project)
    body = await request.json()
    pkgs = (body.get("packages") or "").replace(",", " ").split()
    container = body.get("container") or site["app_container"]
    return job_response(jobs.start(f"Install packages in {container}", project,
                                   lambda j: sites.install_packages(site, j, container, pkgs)))


@app.post("/api/sites/{project}/hosts")
async def api_hosts(project: str):
    site = await require_site(project)
    try:
        sites.hosts_add(site["domain"])
    except PermissionError as exc:
        return bad(str(exc), 403)
    return {"ok": True}


def _check_path(site: dict, container: str, path: str) -> None:
    if container not in {c["name"] for c in site["containers"]}:
        raise HTTPException(400, "Unknown container")
    if not path.startswith("/") or "\x00" in path or len(path) > 1000:
        raise HTTPException(400, "Path must be absolute")


@app.get("/api/sites/{project}/download")
async def api_download(project: str, container: str, path: str):
    site = await require_site(project)
    _check_path(site, container, path)
    probe = await docker.docker("exec", container, "test", "-e", path, timeout=20)
    if not probe.ok:
        raise HTTPException(404, f"{path} does not exist in {container}")
    proc = await asyncio.create_subprocess_exec(docker.docker_bin(), "cp", f"{container}:{path}", "-",
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)

    async def gen():
        assert proc.stdout
        try:
            while chunk := await proc.stdout.read(65536):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    fname = re.sub(r"[^\w.\-]", "_", Path(path).name or "download") + ".tar"
    return StreamingResponse(gen(), media_type="application/x-tar",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/sites/{project}/upload")
async def api_upload(project: str, container: str = Form(...), dest: str = Form(...),
                     file: UploadFile = File(...)):
    site = await require_site(project)
    _check_path(site, container, dest)
    name = re.sub(r"[^\w.\-]", "_", Path(file.filename or "upload").name)
    with tempfile.TemporaryDirectory(dir=config.DATA_DIR) as tmp:
        local = Path(tmp) / name
        with local.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
        target = dest.rstrip("/") + "/" + name if dest.endswith("/") else dest
        r = await docker.docker("cp", str(local), f"{container}:{target}", timeout=600)
        if not r.ok:
            return bad(f"Upload failed: {r.text}")
    if container == site["app_container"] and target.startswith("/home/frappe"):
        await docker.docker("exec", "-u", "root", container, "chown", "1000:1000", target)
    return {"ok": True, "path": target}


# --------------------------------------------------------------------------- terminal

@app.websocket("/ws/terminal/{project}")
async def ws_terminal(ws: WebSocket, project: str, container: str = "", user: str = "frappe",
                      mode: str = "shell"):
    site = await sites.get(project)
    names = {c["name"] for c in site["containers"] if c["running"]} if site else set()
    if not site or container not in names or user not in ("frappe", "root"):
        await ws.close(code=4404)
        return
    await ws.accept()
    command: list[str] = []
    if mode == "console" and container == site["app_container"]:
        command = ["bench", "--site", site["domain"], "console"]
    elif mode == "mariadb" and container == site["app_container"]:
        command = ["bench", "--site", site["domain"], "mariadb"]
    if container != site["app_container"]:
        user = "root"  # db/redis images have no 'frappe' user
    await terminal.bridge(ws, container, user, command)
