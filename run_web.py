#!/usr/bin/env python3
"""Supervise public and optional LAN listeners with exactly one worker runtime."""
import asyncio
from contextlib import contextmanager
import ipaddress
import os
from pathlib import Path
import signal
import ssl
from urllib.parse import urlsplit

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
        host = os.getenv('SANTA_LAN_HOST', '')
        if host:
            address = ipaddress.ip_address(host)
            origin = urlsplit(os.getenv('SANTA_LAN_ORIGIN', ''))
            cert, key = os.getenv('SANTA_LAN_CERT'), os.getenv('SANTA_LAN_KEY')
            port = int(os.getenv('SANTA_LAN_PORT', '8443'))
            if not address.is_private or address.is_unspecified or origin.scheme != 'https' or origin.port != port or not origin.hostname or origin.path != '' or origin.query or origin.fragment or origin.username or origin.password:
                raise ValueError('LAN host must be a specific private IP; LAN origin must be an HTTPS origin with its port.')
            if not cert or not key or not Path(os.getenv('SANTA_OWNER_PASSWORD_FILE') or str(directory / 'owner-password.hash')).is_file():
                raise ValueError('Configure LAN TLS certificate/key and reset the owner password first.')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            servers.append(Listener(uvicorn.Config(create_app(store, service, start_background=False, lan=True),
                           host=host, port=port, ssl_certfile=cert, ssl_keyfile=key, proxy_headers=False, access_log=False)))
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
