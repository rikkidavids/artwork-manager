#!/usr/bin/env python3
"""Verify an Artwork Manager NAS Worker endpoint from Mac or NAS."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

EXPECTED_BUILD = '5.04'
MIN_API = 3


def fetch(url: str, token: str = ''):
    req = urllib.request.Request(url)
    if token:
        req.add_header('X-Artwork-Worker-Token', token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
        return resp.status, json.loads(raw)


def main() -> int:
    if len(sys.argv) < 2:
        print('Usage: python verify_worker.py http://NAS-IP:8765 [API_TOKEN]')
        return 2
    base = sys.argv[1].rstrip('/')
    token = sys.argv[2] if len(sys.argv) > 2 else ''

    try:
        status, root = fetch(base + '/')
    except urllib.error.HTTPError as exc:
        print(f'ERROR: GET / returned HTTP {exc.code}')
        print(exc.read().decode('utf-8', errors='replace'))
        return 1
    except Exception as exc:
        print(f'ERROR: could not reach {base}: {exc}')
        return 1

    print(f'GET / HTTP {status}')
    print(f"worker_build: {root.get('worker_build')!r}")
    print(f"api: {root.get('api')!r}")
    print(f"version: {root.get('version')!r}")
    if root.get('worker_build') != EXPECTED_BUILD:
        print(f'WARNING: expected worker_build {EXPECTED_BUILD}. Rebuild/recreate the Synology Docker project/image, do not only restart it.')
    try:
        api = int(root.get('api') or 0)
    except Exception:
        api = 0
    if api < MIN_API:
        print(f'ERROR: worker API {api or "unknown"} is older than required API {MIN_API}.')
        return 1
    endpoints = root.get('endpoints') or []
    if not any('/scan-library' in str(endpoint) for endpoint in endpoints):
        print('ERROR: worker does not advertise POST /scan-library. Rebuild/recreate the worker project.')
        return 1

    if token:
        try:
            status, auth = fetch(base + '/status', token)
            print(f'GET /status HTTP {status}')
            fs = auth.get('filesystem') or {}
            if fs:
                print('filesystem diagnostics:')
                print(json.dumps(fs, indent=2, sort_keys=True))
        except urllib.error.HTTPError as exc:
            print(f'ERROR: authenticated /status returned HTTP {exc.code}. Check the API token.')
            print(exc.read().decode('utf-8', errors='replace'))
            return 1
        except Exception as exc:
            print(f'ERROR: authenticated /status failed: {exc}')
            return 1
    else:
        print('No API token supplied, so authenticated /status was skipped.')

    print('OK: worker endpoint looks compatible.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
