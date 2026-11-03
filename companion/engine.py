"""Headless engine owned by the Sol desktop. No Python windows or tray."""
from __future__ import annotations

import asyncio
import os
import sys
import threading


async def desktop_lease():
    loop = asyncio.get_running_loop()
    closed = loop.create_future()
    def finish():
        if not closed.done():
            closed.set_result(None)
    def read():
        try:
            sys.stdin.buffer.read(1)
        except (OSError, ValueError):
            pass
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(finish)
            except RuntimeError:
                pass
    threading.Thread(target=read, name="sol-desktop-lease", daemon=True).start()
    await closed


async def serve():
    from pathlib import Path
    browsers = Path(__file__).resolve().parents[1] / "runtime/browsers"
    if browsers.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    import uvicorn
    from .api import create_api
    from .config import DATA

    if not os.environ.get("SOL_DESKTOP_TOKEN"):
        raise RuntimeError("Sol 引擎只能由桌面主程序启动")
    DATA.mkdir(parents=True, exist_ok=True)
    api = create_api()
    port = api.state.service.config.get("app", "port")
    server = uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=port, log_level="warning", access_log=False, timeout_graceful_shutdown=120))
    api.state.request_shutdown = lambda: setattr(server, "should_exit", True)

    # An inherited stdin pipe is the lifetime lease. Parent death closes the pipe;
    # Python gets a chance to stop its Docker services and owned subprocesses.
    async def watch_desktop():
        await desktop_lease()
        server.should_exit = True

    watcher = asyncio.create_task(watch_desktop())
    try:
        await server.serve()
        if not server.started:
            raise RuntimeError(f"本机端口 {port} 无法监听，请检查是否被其他程序占用")
    finally:
        watcher.cancel()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
