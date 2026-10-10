#!/usr/bin/env python3
"""Supervise the public listener with exactly one worker runtime."""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import signal

import uvicorn
from santa_tools import ROOT, load_environment, offline_lock


class Listener(uvicorn.Server):
    # The supervisor owns signals, rather than each listener replacing handlers.
    @contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self):
        pass


async def serve():
    from santa_store import Store
    from santa_service import Service
    from santa_web import create_app
    directory = Path(os.getenv('SANTA_DATA_DIR', str(ROOT / 'runtime'))).expanduser().resolve()
    with offline_lock(directory):
        store = Store(directory)
        service = Service(store)
        servers = [Listener(uvicorn.Config(create_app(store, service, start_background=False),
                    host='127.0.0.1', port=int(os.getenv('SANTA_PUBLIC_PORT', '8000')), workers=1, proxy_headers=False, access_log=False))]
        loop = asyncio.get_running_loop()
        def stop():
            for server in servers:
                server.should_exit = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
        service.start()
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop()
            await asyncio.gather(*tasks, return_exceptions=True)
            await service.stop()


if __name__ == '__main__':
    load_environment()
    os.chdir(ROOT)
    asyncio.run(serve())
