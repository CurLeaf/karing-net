#!/usr/bin/env python3
"""Read-only source/runtime/service checks; --network exercises real paths."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import karing_mac as km
import sync_rules
from tunnel_watch import http_probe, proxy_ports

FAKE = (ipaddress.ip_network('198.18.0.0/15'), ipaddress.ip_network('198.20.0.0/15'))
APPLE_HOSTS = ('apps.apple.com', 'itunes.apple.com', 'icloud.com', 'swscan.apple.com')


def dns_probe(host):
    try:
        p = subprocess.run(['/usr/bin/dig', '+short', '+time=2', '+tries=1', host, 'A'], capture_output=True, text=True, timeout=4)
        addresses = []
        for line in p.stdout.splitlines():
            try:
                addresses.append(ipaddress.ip_address(line.strip()))
            except ValueError:
                pass
        return {'ok': p.returncode == 0 and bool(addresses) and not any(ip in net for ip in addresses for net in FAKE),
                'host': host, 'addresses': [str(a) for a in addresses]}
    except Exception as exc:
        return {'ok': False, 'host': host, 'error': str(exc)}


def connect_tunnel(host, port):
    sock = socket.create_connection(('127.0.0.1', port), timeout=8)
    try:
        sock.sendall(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n'.encode('ascii'))
        head = b''
        while b'\r\n\r\n' not in head and len(head) < 8192:
            byte = sock.recv(1)
            if not byte:
                break
            head += byte
        if not head.startswith(b'HTTP/1.1 200') and not head.startswith(b'HTTP/1.0 200'):
            raise RuntimeError('proxy CONNECT failed: ' + head[:60].decode(errors='replace'))
        return sock
    except Exception:
        sock.close()
        raise


def live_route(host, expected, port):
    try:
        with connect_tunnel(host, port) as sock:
            local_port = str(sock.getsockname()[1])
            for _ in range(5):
                cs = km.connections()
                matches = [c for c in cs if str(c['metadata'].get('sourcePort')) == local_port and c['metadata'].get('host') == host]
                if matches:
                    c = matches[0]
                    return {'ok': expected in c['chains'], 'host': host, 'expected': expected,
                            'chains': c['chains'], 'rule': c.get('rule')}
                time.sleep(.1)
        return {'ok': False, 'host': host, 'error': 'controlled connection not observed'}
    except Exception as exc:
        return {'ok': False, 'host': host, 'error': str(exc)}


def run(network=False, repetitions=1, services=False):
    rows = []
    def add(name, status, detail):
        rows.append({'check': name, 'status': status, 'detail': detail})
        print(f'{status:4} {name}: {json.dumps(detail, ensure_ascii=False)}', flush=True)
    try:
        report = sync_rules.report()
        add('source and generated rules', 'PASS' if report['source_ok'] and report['generated_ok'] else 'FAIL', report)
    except Exception as exc:
        add('rules', 'FAIL', str(exc))
    try:
        version = km.api_json('/version')
        groups = km.GroupReader().read()
        cs = km.connections()
        active_groups = {g for c in cs for g in c['chains'][1:] if g in groups}
        missing = [g for g in active_groups if not groups[g]['now']]
        add('Clash API', 'PASS', {'version': version, 'connections': len(cs)})
        add('active group selection', 'FAIL' if missing else 'PASS', {'missing': missing})
        idle = [g for g, v in groups.items() if not v['now'] and g not in active_groups]
        if idle:
            add('unused group selection', 'WARN', {'not_verified': idle})
    except Exception as exc:
        add('Clash API', 'FAIL', str(exc))
    if services:
        for name, state in [('gc', 'connection-gc.json'), ('watch', 'tunnel-watch.json')]:
            try:
                launch = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/com.karing.net.{name}'], capture_output=True, text=True, timeout=4)
                d = km.load_json(km.STATE_DIR / state)
                age = time.time() - d['at']
                ok = launch.returncode == 0 and '\tstate = running' in launch.stdout and 0 <= age < 90 and d.get('ok', d.get('api_ok', False))
                add(name + ' launchd', 'PASS' if ok else 'FAIL', {'age_seconds': round(age, 1), 'pid': d.get('pid'), 'ticks': d.get('ticks')})
            except Exception as exc:
                add(name + ' launchd', 'FAIL', str(exc))
    if network:
        ports = proxy_ports()
        with ThreadPoolExecutor(max_workers=4) as pool:
            # Submit per-host series to avoid claiming independent DNS cache misses.
            futures = {h: pool.submit(lambda host=h: [dns_probe(host) for _ in range(repetitions)]) for h in APPLE_HOSTS}
            for host, future in futures.items():
                values = future.result()
                add('DNS ' + host, 'PASS' if all(v['ok'] for v in values) else 'FAIL', {'passed': sum(v['ok'] for v in values), 'samples': len(values), 'last': values[-1]})
        urls = [('https://www.apple.com/', ports['mixed_in_rule'], (200, 301, 302)),
                ('https://apps.apple.com/', ports['mixed_in_rule'], (200, 301, 302)),
                ('https://itunes.apple.com/', ports['mixed_in_rule'], (200, 301, 302)),
                ('https://swscan.apple.com/', ports['mixed_in_rule'], (200, 301, 302, 403, 404)),
                ('https://www.gstatic.com/generate_204', ports['mixed_in_rule'], (204,)),
                ('https://www.baidu.com/', ports['mixed_in_direct'], (200, 301, 302))]
        with ThreadPoolExecutor(max_workers=4) as pool:
            fs = [(url, pool.submit(lambda u=url, p=port, ex=expected: [http_probe(u, p, ex) for _ in range(min(5, repetitions))])) for url, port, expected in urls]
            for url, future in fs:
                values = future.result()
                add('HTTP ' + url, 'PASS' if all(v['ok'] for v in values) else 'FAIL', {'passed': sum(v['ok'] for v in values), 'samples': len(values), 'last': values[-1]})
        cases = [(host, 'direct_out') for host in APPLE_HOSTS] + [
            ('api.openai.com', 'urltest_out-GPT自动'), ('gemini.google.com', 'urltest_out-GPT自动'),
            ('api.anthropic.com', 'urltest_out-GPT自动'), ('www.google.com', 'urltest_out'),
            ('github.com', 'urltest_out'), ('www.baidu.com', 'direct_out')]
        for host, expected in cases:
            value = live_route(host, expected, ports['mixed_in_rule'])
            add('live route ' + host, 'PASS' if value['ok'] else 'FAIL', value)
    result = {'at': datetime.now().astimezone().isoformat(), 'rows': rows,
              'passed': sum(r['status'] == 'PASS' for r in rows), 'failed': sum(r['status'] == 'FAIL' for r in rows),
              'warnings': sum(r['status'] == 'WARN' for r in rows)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', action='store_true')
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--services', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 100:
        parser.error('--repetitions must be 1..100')
    result = run(args.network, args.repetitions, args.services)
    if args.output:
        km.atomic_json(args.output, result)
    return int(bool(result['failed']))

if __name__ == '__main__':
    raise SystemExit(main())
