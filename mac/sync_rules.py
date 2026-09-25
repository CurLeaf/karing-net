#!/usr/bin/env python3
"""Merge managed rules without replacing unrelated Karing configuration."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import shutil

import karing_mac as km

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config'
BACKUP = km.STATE_DIR / 'backups'
FILES = ('karing_routing_group.json', 'karing_subscribe_use.json')
ORDER = ['🍎 苹果服务', '🏠 强制直连', '💬 OpenAI', '♊️ Google Gemini', '💬 Claude']
AI = set(ORDER[2:])


def groups_in(data):
    items = data.get('items')
    if not isinstance(items, list):
        raise ValueError('routing.items must be a list')
    custom = [item for item in items if isinstance(item, dict) and item.get('groupid') == 'custom']
    if len(custom) != 1 or not isinstance(custom[0].get('groups'), list):
        raise ValueError('expected one custom routing item')
    groups = custom[0]['groups']
    names = [g.get('name') for g in groups if isinstance(g, dict)]
    if len(names) != len(groups) or any(names.count(name) > 1 for name in ORDER):
        raise ValueError('invalid or duplicate managed routing groups')
    return groups


def rows_in(data):
    rows = data.get('diversion_group')
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('invalid diversion_group')
    for name in ORDER:
        if sum(r.get('diversion_name') == name and r.get('diversion_groupid') == 'custom' for r in rows) > 1:
            raise ValueError('duplicate managed selection')
    return rows


def ordered(items, key):
    # Stable order for unmanaged entries, no deletion/recreation of those entries.
    return sorted(items, key=lambda i: ORDER.index(i[key]) if i.get(key) in ORDER else len(ORDER))


def direct_config():
    path = CONFIG / 'direct.json'
    if not path.exists():
        return None
    data = km.load_json(path)
    domains = data.get('domain_suffix')
    if not isinstance(domains, list) or not domains or any(not isinstance(d, str) or not d.strip('. ') for d in domains):
        raise ValueError('direct.json: domain_suffix must contain domains')
    return {'domain_suffix': sorted(set(d.strip('. ').lower() for d in domains))}


def prepare(routing, use, direct=None):
    routing, use = copy.deepcopy(routing), copy.deepcopy(use)
    groups, rows = groups_in(routing), rows_in(use)
    apple = km.load_json(CONFIG / 'karing_routing_group.apple.json')['items'][0]['groups'][0]
    entry = next((g for g in groups if g.get('name') == apple['name']), None)
    if entry is None:
        entry = copy.deepcopy(apple)
        groups.append(entry)
    else:
        for key in ('domain_suffix', 'rule_set_build_in'):
            entry[key] = list(dict.fromkeys(list(entry.get(key) or []) + apple[key]))
        entry.update({'groupid': 'custom', 'or': True})
    if direct:
        entry = next((g for g in groups if g.get('name') == '🏠 强制直连'), None)
        if entry is None:
            entry = {'name': '🏠 强制直连', 'groupid': 'custom', 'type': '', 'or': True}
            groups.append(entry)
        entry['domain_suffix'] = list(dict.fromkeys(list(entry.get('domain_suffix') or []) + direct['domain_suffix']))
        entry['or'] = True
    names = {g['name'] for g in groups}
    for name in ORDER:
        if name not in names:
            continue
        row = next((r for r in rows if r.get('diversion_name') == name and r.get('diversion_groupid') == 'custom'), None)
        if row is None:
            row = {'diversion_name': name, 'diversion_groupid': 'custom', 'dns_servers': []}
            rows.append(row)
        row.update({'server_groupid': 'urltest' if name in AI else 'direct',
                    'server_name': 'GPT自动' if name in AI else 'direct_out'})
    groups[:] = ordered(groups, 'name')
    rows[:] = ordered(rows, 'diversion_name')
    return routing, use


def issues(routing, use, direct=None):
    desired = prepare(routing, use, direct)
    return [name for name, before, after in zip(FILES, (routing, use), desired) if before != after]


def core_issues(core, routing):
    """Check generated config separately; this does not prove the live core loaded it."""
    problems = []
    route_rules = core.get('route', {}).get('rules', [])
    dns_rules = core.get('dns', {}).get('rules', [])
    tags = {ob.get('tag') for ob in core.get('outbounds', [])}
    def suffixes(rule):
        result = set(rule.get('domain_suffix') or [])
        for child in rule.get('rules') or []:
            result.update(suffixes(child))
        return result
    for group in groups_in(routing):
        name = group['name']
        if name not in ORDER:
            continue
        tag = 'urltest_out-GPT自动' if name in AI else 'direct_out'
        rule = next((r for r in route_rules if r.get('name') == name + '[自定义]'), None)
        if not rule or rule.get('outbound') != tag or tag not in tags:
            problems.append(f'{name}: generated route missing/mismatched')
        if rule and name not in AI:
            if not set(group.get('domain_suffix') or []) <= suffixes(rule):
                problems.append(f'{name}: generated domains missing')
            dns = next((r for r in dns_rules if name + '[自定义]' in r.get('name', '')), {})
            if dns.get('server') != 'dns_direct_out':
                problems.append(f'{name}: generated DNS not direct')
    indices = {r.get('name'): n for n, r in enumerate(route_rules)}
    if indices.get('🍎 苹果服务[自定义]', float('inf')) >= indices.get('🌏 国外穿墙[自定义]', float('inf')):
        problems.append('Apple route must precede foreign route')
    return problems


def report():
    routing, use = [km.load_json(km.KARING_DIR / n) for n in FILES]
    source = issues(routing, use, direct_config())
    generated = core_issues(km.load_json(km.KARING_DIR / 'service_core.json'), routing)
    return {'source_ok': not source, 'source_drift': source, 'generated_ok': not generated,
            'generated_drift': generated, 'live_reload_verified': False}


def backup(paths):
    BACKUP.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = BACKUP / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    dest.mkdir(mode=0o700)
    for path in paths:
        shutil.copy2(path, dest / path.name)
        (dest / path.name).chmod(0o600)
    return dest


def apply():
    with km.InstanceLock('sync-rules'):
        paths = [km.KARING_DIR / name for name in FILES]
        original = [p.read_bytes() for p in paths]
        inputs = [json.loads(raw) for raw in original]
        desired = prepare(*inputs, direct_config())
        changed = [i for i in range(2) if desired[i] != inputs[i]]
        if not changed:
            return {'changed': [], 'backup': None, 'reconnect_required': False}
        # Do not mutate the output config or request /reload from the Mac extension.
        dest = backup(paths + [km.KARING_DIR / 'service_core.json'])
        if [p.read_bytes() for p in paths] != original:
            raise RuntimeError('Karing changed config while preparing; retry when stable')
        written = []
        try:
            for i in changed:
                if paths[i].read_bytes() != original[i]:
                    raise RuntimeError('concurrent Karing write; sync aborted')
                km.atomic_json(paths[i], desired[i])
                written.append(i)
            if [km.load_json(p) for p in paths] != list(desired):
                raise RuntimeError('Karing changed files during verification')
        except Exception:
            for i in written:
                # Avoid overwriting a newer App write during rollback.
                if km.load_json(paths[i]) == desired[i]:
                    km.atomic_json(paths[i], inputs[i])
            raise
        for old in sorted(BACKUP.iterdir())[:-20]:
            if old.is_dir() and all(p.name in (*FILES, 'service_core.json') for p in old.iterdir()):
                shutil.rmtree(old)
        return {'changed': [FILES[i] for i in changed], 'backup': str(dest), 'reconnect_required': True}


def restore(directory: str):
    dest = Path(directory).expanduser().resolve()
    if dest.parent != BACKUP.resolve() or not dest.is_dir():
        raise ValueError('restore requires a backup directory under ' + str(BACKUP))
    data = [km.load_json(dest / name) for name in FILES]
    groups_in(data[0]); rows_in(data[1])
    with km.InstanceLock('sync-rules'):
        saved = backup([km.KARING_DIR / name for name in FILES])
        for name, value in zip(FILES, data):
            km.atomic_json(km.KARING_DIR / name, value)
    return {'restored': str(dest), 'undo_backup': str(saved), 'reconnect_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'apply', 'restore'])
    parser.add_argument('--backup', help='backup directory for restore')
    args = parser.parse_args()
    try:
        result = report() if args.command == 'check' else (apply() if args.command == 'apply' else restore(args.backup or ''))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(args.command == 'check' and not (result['source_ok'] and result['generated_ok']))
    except Exception as exc:
        print(str(exc), file=__import__('sys').stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
