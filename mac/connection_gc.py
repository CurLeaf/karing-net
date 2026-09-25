#!/usr/bin/env python3
"""macOS connection collector: measured quiet time, fail-closed snapshots."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import quote

import karing_mac as km

POLL_SECONDS = 8.0
QUIET_SECONDS = 24.0
ACTIVE_BYTES = 2048
CLOSE_BUDGET = 24
PROTECTED = {'direct_out', 'block_out', 'dns_direct_out', 'dns_proxy_out'}


def age(start: str) -> float:
    try:
        value = start.replace('Z', '+00:00')
        # Clash emits nanosecond timestamps; Python 3.9 parses only six
        # fractional digits, so truncate excess precision before parsing.
        value = re.sub(r'\.(\d{6})\d+(?=(?:[+-]\d{2}:?\d{2})$)', r'.\1', value)
        value = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', value)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def protected(c: dict) -> bool:
    meta = c.get('metadata') or {}
    return (meta.get('network') != 'tcp' or str(meta.get('destinationPort')) in {'22', '2222', '3389'}
            or 'mixed_in_proxy' in meta.get('type', '') or 'mixed_in_direct' in meta.get('type', '')
            or meta.get('protocol') == 'ssh'
            or bool(re.search(r'/(?:ssh|mosh-client)(?:\s|$)', meta.get('processPath', ''))))


def direct_suffixes() -> tuple[str, ...]:
    """Only explicitly forced domains present in both source and generated rules."""
    routing = km.load_json(km.KARING_DIR / 'karing_routing_group.json')
    use = km.load_json(km.KARING_DIR / 'karing_subscribe_use.json')
    core = km.load_json(km.KARING_DIR / 'service_core.json')
    mapped = {r.get('diversion_name') for r in use.get('diversion_group', [])
              if r.get('server_name') == 'direct_out' and r.get('server_groupid') == 'direct'}
    def suffixes(value):
        out = set(value.get('domain_suffix') or [])
        for rule in value.get('rules') or []:
            out.update(suffixes(rule))
        return out
    enabled = set()
    for item in routing.get('items', []):
        for group in item.get('groups', []):
            name = group.get('name')
            if name not in {'🏠 强制直连', '🍎 苹果服务'} or name not in mapped:
                continue
            for rule in core.get('route', {}).get('rules', []):
                if rule.get('name') == name + '[自定义]' and rule.get('outbound') == 'direct_out':
                    enabled.update(set(group.get('domain_suffix', [])) & suffixes(rule))
    return tuple(sorted(s.lower().strip('. ') for s in enabled if isinstance(s, str) and s.strip('. ')))


def classification(c, groups, suffixes=()):
    chains = c.get('chains') or []
    if protected(c) or len(chains) < 2 or chains[0] in PROTECTED:
        return None
    owners = [g for g in chains[1:] if g in groups]
    if not owners or any(not groups[g]['now'] for g in owners):
        return None
    # Unknown chain elements may be nested groups that cannot be resolved safely.
    if any(g not in groups for g in chains[1:]):
        return None
    host = str(c.get('metadata', {}).get('host') or '').lower().rstrip('.')
    if age(c.get('start')) >= 1 and any(host == d or host.endswith('.' + d) for d in suffixes):
        return 'force-direct-misroute'
    if chains[0] in km.selected_nodes(groups):
        return None
    if not isinstance(c.get('start'), str) or age(c['start']) < 3:
        return None
    return 'quiet-old-node'


@dataclass
class Sample:
    identity: tuple
    upload: int
    download: int
    at: float
    quiet_since: float
    reason: str | None
    selection: tuple


class TrafficBook:
    def __init__(self):
        self.samples = {}

    def reset(self):
        self.samples.clear()

    def observe(self, groups, conns, now, suffixes=()):
        active = km.selected_nodes(groups)
        if not active:
            self.reset()
            return []
        present = set()
        candidates = []
        for c in conns:
            cid = c['id']
            present.add(cid)
            reason = classification(c, groups, suffixes)
            identity = (c.get('start'), tuple(c['chains']))
            selection = tuple((g, groups[g]['now']) for g in c['chains'][1:] if g in groups)
            previous = self.samples.get(cid)
            upload, download = c['upload'], c['download']
            quiet = now
            if (previous and previous.identity == identity and reason == previous.reason == 'quiet-old-node'
                    and previous.selection == selection):
                dt = now - previous.at
                up, down = upload - previous.upload, download - previous.download
                if 0 < dt <= 2 * POLL_SECONDS and up >= 0 and down >= 0:
                    if (up + down) / dt < ACTIVE_BYTES / POLL_SECONDS:
                        quiet = previous.quiet_since
            self.samples[cid] = Sample(identity, upload, download, now, quiet, reason, selection)
            if reason == 'force-direct-misroute' or (reason == 'quiet-old-node' and now - quiet >= QUIET_SECONDS):
                candidates.append((c, reason))
        self.samples = {cid: s for cid, s in self.samples.items() if cid in present}
        return sorted(candidates, key=lambda x: x[1] != 'force-direct-misroute')[:CLOSE_BUDGET]


def still_eligible(original, current, reason, groups, suffixes):
    if current is None or original.get('start') != current.get('start') or original['chains'] != current['chains']:
        return False
    if classification(current, groups, suffixes) != reason:
        return False
    # Recheck protects a connection that began transferring after the sample.
    return (reason == 'force-direct-misroute'
            or (original['upload'], original['download']) == (current['upload'], current['download']))


class Collector:
    def __init__(self, apply=False):
        self.apply = apply
        self.reader = km.GroupReader()
        self.book = TrafficBook()
        self.log = km.logger('connection-gc' if apply else 'connection-gc-dry-run')
        self.state_file = km.STATE_DIR / ('connection-gc.json' if apply else 'connection-gc-dry-run.json')
        self.totals = {'delete_accepted': 0, 'confirmed_absent': 0, 'delete_failed': 0, 'api_errors': 0}
        if apply and self.state_file.exists():
            try:
                prior = km.load_json(self.state_file).get('totals', {})
                self.totals.update({k: int(prior.get(k, 0)) for k in self.totals})
            except (OSError, ValueError, TypeError):
                pass
        self.ticks = 0
        self.previous = None
        self.last_log = 0
        self.was_ok = True

    def step(self):
        started = time.monotonic()
        try:
            groups = self.reader.read()
            conns = km.connections()
            suffixes = direct_suffixes()
            selected = {k: v['now'] for k, v in groups.items() if v['now']}
            found = self.book.observe(groups, conns, time.monotonic(), suffixes)
            accepted = []
            if self.apply and found:
                fresh_groups = self.reader.read()
                fresh = {c['id']: c for c in km.connections()}
                refreshed = time.monotonic()
                for original, reason in found:
                    if km.STOP.is_set() or time.monotonic() - refreshed > 2:
                        break
                    cid = original['id']
                    if not still_eligible(original, fresh.get(cid), reason, fresh_groups, suffixes):
                        continue
                    try:
                        km.api_request('/connections/' + quote(cid, safe=''), 'DELETE')
                        self.totals['delete_accepted'] += 1
                        accepted.append(cid)
                        self.book.samples.pop(cid, None)
                        self.log.info('delete accepted id=%s reason=%s', cid, reason)
                    except Exception:
                        self.totals['delete_failed'] += 1
                        raise
                if accepted:
                    remaining = {c['id'] for c in km.connections()}
                    self.totals['confirmed_absent'] += sum(cid not in remaining for cid in accepted)
            self.ticks += 1
            state = {'at': time.time(), 'pid': __import__('os').getpid(), 'ok': True,
                     'mode': 'apply' if self.apply else 'dry-run', 'ticks': self.ticks,
                     'selected': selected, 'connections': len(conns), 'samples': len(self.book.samples),
                     'candidates': [{'id': c['id'], 'reason': reason} for c, reason in found],
                     'accepted_this_tick': accepted, 'totals': self.totals,
                     'elapsed_ms': round((time.monotonic() - started) * 1000)}
            km.atomic_json(self.state_file, state)
            if found or selected != self.previous or not self.was_ok or started - self.last_log >= 120:
                self.log.info('tick=%s mode=%s connections=%s candidates=%s deleted=%s groups=%s',
                              self.ticks, state['mode'], len(conns), len(found), len(accepted), len(selected))
                self.last_log = started
            self.previous, self.was_ok = selected, True
            return True
        except Exception as exc:
            # Never count wall-clock outage/sleep as quiet samples.
            self.book.reset()
            self.totals['api_errors'] += 1
            if self.was_ok or started - self.last_log >= 60:
                self.log.warning('paused; fresh samples required: %s', exc)
                self.last_log = started
            self.was_ok = False
            km.atomic_json(self.state_file, {'at': time.time(), 'ok': False, 'pid': __import__('os').getpid(),
                'mode': 'apply' if self.apply else 'dry-run', 'error': str(exc), 'totals': self.totals})
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true', help='observe only (default)')
    parser.add_argument('--loop', action='store_true', help='retain samples across 8-second ticks')
    parser.add_argument('--duration', type=float, help='stop a loop after N seconds')
    args = parser.parse_args()
    km.signals()
    try:
        with km.InstanceLock('connection-gc' if args.apply else 'connection-gc-dry-run'):
            collector = Collector(args.apply)
            collector.log.info('started pid=%s mode=%s', __import__('os').getpid(), 'apply' if args.apply else 'dry-run')
            deadline = time.monotonic() + args.duration if args.duration else float('inf')
            def step():
                ok = collector.step()
                if time.monotonic() + POLL_SECONDS >= deadline:
                    km.STOP.set()
                return ok
            result = km.run_loop(step, POLL_SECONDS, once=not args.loop)
            if not args.loop:
                print(json.dumps(km.load_json(collector.state_file), ensure_ascii=False))
            return result
    except RuntimeError as exc:
        print(str(exc), file=__import__('sys').stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
