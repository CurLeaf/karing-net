"""macOS-only runtime utilities; no dependency on the Linux directory."""
from __future__ import annotations

import fcntl
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
KARING_DIR = Path(os.environ.get('KARING_DATA_DIR', str(Path.home() / 'Library/Group Containers/group.com.nebula.karing'))).expanduser()
STATE_DIR = Path(os.environ.get('KARING_STATE_DIR', str(Path.home() / 'Library/Application Support/karing-net'))).expanduser()
SERVICE_JSON = KARING_DIR / 'service.json'
GROUP_TYPES = {'urltest', 'url-test', 'selector'}
STOP = threading.Event()


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f'{path.name}: expected object')
    return data


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), mode)
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def service_config() -> dict:
    return load_json(SERVICE_JSON)


def api_request(path: str, method: str = 'GET', body: bytes | None = None,
                timeout: float = 4.0):
    """Connect directly to loopback, never through macOS system proxy settings."""
    cfg = service_config()
    port = int(cfg.get('control_port') or 3057)
    if not 1 <= port <= 65535 or not path.startswith('/'):
        raise ValueError('invalid API endpoint')
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers={
            'Authorization': 'Bearer ' + str(cfg.get('secret') or ''),
            'Content-Type': 'application/json',
        })
        response = conn.getresponse()
        raw = response.read(8 * 1024 * 1024 + 1)
        if response.status not in (200, 204):
            raise RuntimeError(f'Clash API {method} {path}: HTTP {response.status}')
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('Clash API response exceeded size limit')
        return response.status, json.loads(raw) if raw else None
    finally:
        conn.close()


def api_json(path: str):
    return api_request(path)[1]


def proxy_path(name: str) -> str:
    return '/proxies/' + quote(name, safe='')


def select_proxy(group: str, target: str) -> None:
    """Select a member of a Clash-compatible proxy group through the API."""
    if not isinstance(group, str) or not group or not isinstance(target, str) or not target:
        raise ValueError('proxy group and target are required')
    body = json.dumps({'name': target}, ensure_ascii=False, separators=(',', ':')).encode()
    api_request(proxy_path(group), 'PUT', body)


def connections() -> list[dict]:
    payload = api_json('/connections')
    if not isinstance(payload, dict) or not isinstance(payload.get('connections'), list):
        raise ValueError('invalid connections snapshot')
    result = payload['connections']
    ids = set()
    for c in result:
        if not isinstance(c, dict) or not isinstance(c.get('id'), str) or not c['id'] or c['id'] in ids:
            raise ValueError('invalid/duplicate connection identity')
        ids.add(c['id'])
        if not isinstance(c.get('metadata'), dict) or not isinstance(c.get('chains'), list):
            raise ValueError('invalid connection metadata/chains')
        if not all(isinstance(x, str) for x in c['chains']):
            raise ValueError('invalid chain names')
        if not all(isinstance(c.get(k), int) and c[k] >= 0 for k in ('upload', 'download')):
            raise ValueError('invalid connection traffic counters')
    return result


class GroupReader:
    """Discover rarely; refresh each group on every poll. Empty idle groups are valid."""
    def __init__(self):
        self.names = []
        self.discovered = -float('inf')

    def read(self, force: bool = False) -> dict:
        now = time.monotonic()
        if force or not self.names or now - self.discovered >= 300:
            payload = api_json('/proxies')
            if not isinstance(payload, dict) or not isinstance(payload.get('proxies'), dict):
                raise ValueError('invalid proxies snapshot')
            self.names = sorted(k for k, v in payload['proxies'].items()
                                if isinstance(v, dict) and str(v.get('type', '')).lower() in GROUP_TYPES
                                and k != 'GLOBAL')
            self.discovered = now
        groups = {}
        for name in self.names:
            data = api_json(proxy_path(name))
            if not isinstance(data, dict) or str(data.get('type', '')).lower() not in GROUP_TYPES:
                self.discovered = -float('inf')
                raise ValueError(f'group unavailable: {name}')
            if not isinstance(data.get('now'), str) or not isinstance(data.get('all'), list):
                raise ValueError(f'incomplete group: {name}')
            if data['now'] and data['now'] not in data['all']:
                raise ValueError(f'inconsistent selected node: {name}')
            groups[name] = data
        if not groups or not any(g['now'] for g in groups.values()):
            raise ValueError('no selected groups; pause collection')
        return groups


def selected_nodes(groups: dict) -> set[str]:
    # Resolve nested selectors conservatively; cycles/empty nested selectors fail closed.
    result = set()
    def visit(name, seen):
        if name in seen:
            raise ValueError('selector cycle')
        if name not in groups:
            result.add(name)
        elif groups[name]['now']:
            visit(groups[name]['now'], seen | {name})
        else:
            raise ValueError('selected nested group has no node')
    for group in groups.values():
        if group['now']:
            visit(group['now'], set())
    return result


class InstanceLock:
    def __init__(self, name: str):
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (STATE_DIR / (name + '.pid')).open('a+')
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError(f'{name} already running') from None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def logger(name: str) -> logging.Logger:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        handler = RotatingFileHandler(STATE_DIR / (name + '.log'), maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        log.addHandler(handler)
    return log


def signals():
    STOP.clear()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())


def run_loop(step, interval: float, once: bool = False) -> int:
    failures = 0
    while not STOP.is_set():
        ok = step()
        if once:
            return 0 if ok else 2
        failures = 0 if ok else failures + 1
        STOP.wait(interval if ok else min(60, interval * 2 ** min(failures, 3)))
    return 0
