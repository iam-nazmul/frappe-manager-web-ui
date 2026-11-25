"""Interactive `docker exec` shell bridged to the browser (xterm.js) through a PTY."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios

from fastapi import WebSocket, WebSocketDisconnect

from . import docker


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def bridge(ws: WebSocket, container: str, user: str, command: list[str]) -> None:
    master, slave = pty.openpty()
    _set_size(master, 30, 120)
    shell = ("if command -v bash >/dev/null 2>&1; then exec bash -l; else exec sh; fi"
             if not command else None)
    args = [docker.docker_bin(), "exec", "-it", "-u", user, "-e", "TERM=xterm-256color"]
    if user != "root":
        args += ["-w", "/home/frappe/frappe-bench"]
    args += [container, *(command or ["sh", "-c", shell])]
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=docker.proc_env())
    os.close(slave)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def readable() -> None:
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        queue.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, readable)

    async def pump_out() -> None:
        while True:
            data = await queue.get()
            if not data:
                break
            await ws.send_bytes(data)

    async def pump_in() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            if msg.get("bytes"):
                os.write(master, msg["bytes"])
            elif msg.get("text"):
                text = msg["text"]
                if text.startswith("\x00resize:"):
                    try:
                        size = json.loads(text[8:])
                        _set_size(master, int(size["rows"]), int(size["cols"]))
                        if proc.returncode is None:
                            os.killpg(proc.pid, signal.SIGWINCH)
                    except (ValueError, KeyError, ProcessLookupError, PermissionError):
                        pass
                else:
                    os.write(master, text.encode())

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    try:
        await asyncio.wait({out_task, in_task, asyncio.create_task(proc.wait())},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (out_task, in_task):
            t.cancel()
        try:
            loop.remove_reader(master)
        except (ValueError, OSError):
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 3)
            except (ProcessLookupError, asyncio.TimeoutError):
                proc.kill()
        os.close(master)
        try:
            await ws.send_text("\r\n\x1b[33m[session closed]\x1b[0m\r\n")
            await ws.close()
        except Exception:  # noqa: BLE001 - socket already gone
            pass
