"""ERPNext image versions from Docker Hub (cached, with an offline fallback)."""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
FALLBACK = [
    "v15.80.1", "v15.80.0", "v15.79.2", "v15.79.1", "v15.79.0", "v15.78.1", "v15.78.0",
    "v15.77.0", "v15.76.0", "v15.75.1", "v15.75.0", "v15.74.0", "v15.73.2", "v15.63.0",
]
_cache: dict = {"at": 0.0, "versions": [], "source": ""}


def _key(tag: str) -> tuple[int, int, int]:
    m = TAG_RE.match(tag)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)  # type: ignore[return-value]


def _fetch() -> list[str]:
    url = "https://hub.docker.com/v2/repositories/frappe/erpnext/tags?page_size=100&ordering=last_updated"
    tags: set[str] = set()
    for _ in range(3):  # up to 300 most recently pushed tags
        req = urllib.request.Request(url, headers={"User-Agent": "frappe-docker-manager-ui"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - fixed https URL
            data = json.load(resp)
        tags.update(t["name"] for t in data.get("results", []) if TAG_RE.match(t.get("name", "")))
        url = data.get("next")
        if not url:
            break
    return sorted(tags, key=_key, reverse=True)


async def list_versions(force: bool = False) -> dict:
    if not force and _cache["versions"] and time.time() - _cache["at"] < 3600:
        return _cache
    try:
        versions = await asyncio.to_thread(_fetch)
        source = "docker-hub"
    except Exception:  # noqa: BLE001 - offline or rate limited
        versions, source = [], ""
    if not versions:
        versions, source = FALLBACK, "fallback"
    _cache.update(at=time.time(), versions=versions[:150], source=source)
    return _cache


def is_valid(tag: str) -> bool:
    return bool(TAG_RE.match(tag)) or tag in {"latest", "version-15", "version-16", "develop"}
