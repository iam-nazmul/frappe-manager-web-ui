"""Background jobs with live, replayable logs (consumed via Server-Sent Events)."""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import config


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s" if m else f"{s}s"


class JobError(Exception):
    """Raised inside a job to stop it with a clean, user-facing message."""


@dataclass
class Job:
    id: str
    title: str
    scope: str  # "traefik", "system" or a site project name
    status: str = "running"  # running | success | failed
    lines: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: dict = field(default_factory=dict)
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    async def log(self, text: str, level: str = "info") -> None:
        for part in str(text).splitlines() or [""]:
            self.lines.append({"t": round(time.time() - self.started, 1), "level": level, "text": part})
        async with self._cond:
            self._cond.notify_all()

    async def step(self, text: str) -> None:
        await self.log(text, "step")

    async def ok(self, text: str) -> None:
        await self.log(text, "ok")

    async def warn(self, text: str) -> None:
        await self.log(text, "warn")

    async def error(self, text: str) -> None:
        await self.log(text, "error")

    async def wait_new(self, seen: int, timeout: float = 15) -> None:
        async with self._cond:
            if len(self.lines) > seen or self.status != "running":
                return
            try:
                await asyncio.wait_for(self._cond.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "scope": self.scope,
            "status": self.status,
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "duration": round(self.elapsed),
            "elapsed": round(self.elapsed, 1),
            "duration_text": fmt_duration(self.elapsed),
            "result": self.result,
        }


JobFn = Callable[[Job], Awaitable[dict | None]]

_jobs: dict[str, Job] = {}
_scope_locks: dict[str, asyncio.Lock] = {}
_tasks: set[asyncio.Task] = set()


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def recent(limit: int = 20, scope: str | None = None) -> list[Job]:
    items = [j for j in _jobs.values() if scope is None or j.scope == scope]
    return sorted(items, key=lambda j: j.started, reverse=True)[:limit]


def running_for(scope: str) -> Job | None:
    for j in _jobs.values():
        if j.scope == scope and j.status == "running":
            return j
    return None


def start(title: str, scope: str, fn: JobFn) -> Job:
    """Start ``fn`` in the background. Jobs sharing a scope run one at a time."""
    job = Job(id=uuid.uuid4().hex[:12], title=title, scope=scope)
    _jobs[job.id] = job
    lock = _scope_locks.setdefault(scope, asyncio.Lock())

    async def runner() -> None:
        if lock.locked():
            await job.warn("Another task for this target is running - queued until it finishes...")
        async with lock:
            try:
                res = await fn(job)
                job.result = res or {}
                job.finished = time.time()
                job.status = "success"
                await job.ok(f"✔ Finished successfully in {fmt_duration(job.elapsed)}")
            except JobError as exc:
                job.finished = time.time()
                job.status = "failed"
                await job.error(f"✖ {exc}")
                await job.error(f"✖ Failed after {fmt_duration(job.elapsed)}")
            except Exception as exc:  # noqa: BLE001 - surface anything to the UI
                job.finished = time.time()
                job.status = "failed"
                await job.error(f"✖ Unexpected error: {exc}")
                await job.log(traceback.format_exc(), "debug")
                await job.error(f"✖ Failed after {fmt_duration(job.elapsed)}")
            finally:
                job.finished = job.finished or time.time()
                async with job._cond:
                    job._cond.notify_all()
                _persist(job)

    task = asyncio.get_running_loop().create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


def _persist(job: Job) -> None:
    try:
        path = config.JOBS_DIR / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(job.started))}-{job.id}.log"
        with path.open("w") as fh:
            fh.write(f"# {job.title} [{job.scope}] -> {job.status} in {fmt_duration(job.elapsed)}\n")
            for line in job.lines:
                fh.write(f"[{line['t']:>7}s] {line['level']:<5} {line['text']}\n")
    except OSError:
        pass
