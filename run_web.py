#!/usr/bin/env python3
"""Start the single-process Santa web service with optional local .env settings."""
import os
from pathlib import Path
import uvicorn

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    env_file = root / '.env'
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith('#'):
                key, separator, value = line.partition('=')
                if not separator or not key.strip().replace('_', '').isalnum():
                    raise SystemExit('Invalid .env line; use KEY=value without export.')
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))
    os.chdir(root)
    uvicorn.run('santa_web:app', host='127.0.0.1', port=8000, workers=1,
                proxy_headers=False, access_log=False)
