#!/usr/bin/env python3
"""Monitor Karing API, rules and proxy/direct paths with hysteresis."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import time

import karing_mac as km
import sync_rules

INTERVAL = 10
PROBE_INTERVAL = 30


def proxy_ports():
    core = km.load_json(km.KARING_DIR / 'service_core.json')
    result = {i.get('tag'): i.get('listen_port') for i in core.get('inbounds', []) if i.get('type') == 'mixed'}
    for tag in ('mixed_in_direct', 'mixed_in_rule'):
        if not isinstance(result.get(tag), int) or not 1 <= result[tag] <= 65535:
            raise ValueError(f'missing inbound port: {tag}')
    return result


def http_probe(url, port=None, expected=(200, 204, 301, 302)):
    args = ['/usr/bin/curl', '--silent', '--show-error', '--output', '/dev/null',
            '--connect-timeout', '4', '--max-time', '8', '--write-out', '%{http_code} %{time_total}',
            '--proxy', f'http://127.0.0.1:{port}' if port else '', '--noproxy', '' if port else '*', url]
    at = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        code, elapsed = (proc.stdout.strip().split() + ['0', '0'])[:2]
        ok = proc.returncode == 0 and int(code) in expected
        return {'ok': ok, 'http': int(code), 'seconds': round(float(elapsed), 3),
                'error': '' if ok else f'curl={proc.returncode} HTTP={code} {proc.stderr.strip()[:120]}'}
    except Exception as exc:
        return {'ok': False, 'http': 0, 'seconds': round(time.monotonic() - at, 3), 'error': str(exc)[:160]}


class Health:
    def __init__(self):
        self.state, self.failures, self.successes = 'unknown', 0, 0

    def note(self, ok):
        before = self.state
        self.successes = self.successes + 1 if ok else 0
        self.failures = self.failures + 1 if not ok else 0
        if self.failures >= 3:
            self.state = 'down'
        elif self.successes >= 2:
            self.state = 'up'
        return ('down' if self.state == 'down' else 'recovered') if self.state != before and (self.state == 'down' or before == 'down') else None

    def snapshot(self):
        return {'state': self.state, 'failures': self.failures, 'successes': self.successes}


def notify(title, body):
    # Pass data as argv, never interpolate it into AppleScript source.
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        return subprocess.run(['/usr/bin/osascript', '-e', script, title, body], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def network_snapshot():
    result = {}
    for name, cmd in [('routes', ['/usr/sbin/netstat', '-rn']), ('dns', ['/usr/sbin/scutil', '--dns']),
                      ('proxy', ['/usr/sbin/scutil', '--proxy'])]:
        try:
            result[name] = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout[:12000]
        except (OSError, subprocess.TimeoutExpired) as exc:
            result[name] = str(exc)
    km.atomic_json(km.STATE_DIR / 'network-snapshot.json', {'at': time.time(), **result})


class Watcher:
    def __init__(self, notifications=True):
        self.reader = km.GroupReader()
        self.log = km.logger('tunnel-watch')
        self.health = {name: Health() for name in ('api', 'proxy', 'direct')}
        self.previous = None
        self.last_probe = self.last_rules = -float('inf')
        self.last_log = self.last_error = -float('inf')
        self.probes = {}
        self.rules = {}
        self.ticks = 0
        self.notifications = notifications
        self.last_notify = {}

    def transition(self, name, ok, detail):
        event = self.health[name].note(ok)
        if event:
            self.log.warning('%s %s: %s', name, event, detail)
            network_snapshot()
            key = (name, event)
            now = time.monotonic()
            if self.notifications and now - self.last_notify.get(key, -float('inf')) >= 60:
                self.last_notify[key] = now
                if not notify('Karing 线路状态', f'{name}: {"恢复" if event == "recovered" else "持续异常"}；{detail}'):
                    self.log.warning('notification unavailable; event kept in log')

    def step(self):
        now = time.monotonic()
        error, selected, count = '', {}, None
        try:
            groups = self.reader.read()
            conns = km.connections()
            selected = {name: group['now'] for name, group in groups.items() if group['now']}
            count = len(conns)
            self.transition('api', True, 'Clash API 可用')
            if selected != self.previous:
                self.log.info('selection changed: %s', json.dumps(selected, ensure_ascii=False))
                self.previous = selected
        except Exception as exc:
            error = str(exc)
            self.transition('api', False, error)
            if now - self.last_error >= 60:
                self.log.warning('API paused: %s', error)
                self.last_error = now
        if now - self.last_probe >= PROBE_INTERVAL:
            self.last_probe = now
            try:
                ports = proxy_ports()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(http_probe, 'https://www.gstatic.com/generate_204', ports['mixed_in_rule'], (204,))
                    b = pool.submit(http_probe, 'https://www.baidu.com/', ports['mixed_in_direct'])
                    self.probes = {'proxy': a.result(), 'direct': b.result()}
                for name, result in self.probes.items():
                    detail = result['error'] or 'HTTP 探测成功'
                    if not result['ok'] and self.health[name].failures == 2:
                        secondary = http_probe(
                            'https://cp.cloudflare.com/generate_204' if name == 'proxy' else 'https://www.apple.com/',
                            ports['mixed_in_rule' if name == 'proxy' else 'mixed_in_direct'],
                            (204,) if name == 'proxy' else (200, 301, 302))
                        result['secondary'] = secondary
                        detail = ('仅主探测目标异常；备用目标可达' if secondary['ok']
                                  else '主、备用探测目标均异常') + '；' + detail
                    self.transition(name, result['ok'], detail)
                if self.probes['proxy']['ok'] != self.probes['direct']['ok']:
                    self.log.info('path divergence proxy=%s direct=%s', self.probes['proxy']['ok'], self.probes['direct']['ok'])
            except Exception as exc:
                for name in ('proxy', 'direct'):
                    self.transition(name, False, str(exc))
        if now - self.last_rules >= 120:
            self.last_rules = now
            try:
                self.rules = sync_rules.report()
                if not self.rules['source_ok'] or not self.rules['generated_ok']:
                    self.log.warning('rule drift: %s', json.dumps(self.rules, ensure_ascii=False))
            except Exception as exc:
                self.rules = {'error': str(exc)}
                self.log.warning('rule audit failed: %s', exc)
        self.ticks += 1
        km.atomic_json(km.STATE_DIR / 'tunnel-watch.json', {
            'at': time.time(), 'pid': os.getpid(), 'ticks': self.ticks, 'api_ok': not error, 'error': error,
            'selected': selected, 'connections': count, 'health': {k: v.snapshot() for k, v in self.health.items()},
            'probes': self.probes, 'rules': self.rules,
        })
        if now - self.last_log >= 120:
            self.log.info('heartbeat ticks=%s connections=%s health=%s', self.ticks, count,
                          {k: v.state for k, v in self.health.items()})
            self.last_log = now
        return not error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    args = parser.parse_args()
    km.signals()
    try:
        with km.InstanceLock('tunnel-watch'):
            watcher = Watcher(not args.no_notify)
            watcher.log.info('started pid=%s', os.getpid())
            return km.run_loop(watcher.step, INTERVAL, once=args.once)
    except RuntimeError as exc:
        print(str(exc), file=__import__('sys').stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
