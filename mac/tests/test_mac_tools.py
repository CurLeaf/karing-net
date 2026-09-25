"""Offline acceptance: never reads or modifies the user's Karing configuration."""
from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import karing_mac as km
import connection_gc as gc
import sync_rules as sync
import tunnel_watch as watch


def groups(current='new'):
    return {'pool': {'now': current, 'all': ['old', 'new'], 'type': 'URLTest'}}


def conn(cid='c', node='old', up=900000, down=100000, port='443', network='tcp'):
    return {'id': cid, 'chains': [node, 'pool'], 'upload': up, 'download': down,
            'start': (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat(),
            'metadata': {'host': 'site.example', 'destinationPort': port, 'network': network}}


class TrafficTests(unittest.TestCase):
    def test_switch_starts_quiet_window_at_detection(self):
        book = gc.TrafficBook(); c = conn()
        for t in (0, 8, 16): book.observe(groups('old'), [c], t)
        for t in (24, 32, 40): self.assertEqual(book.observe(groups('new'), [c], t), [])
        self.assertEqual(len(book.observe(groups('new'), [c], 48)), 1)

    def test_explicit_proxy_inbound_is_protected(self):
        c = conn(); c['metadata']['type'] = 'mixed/mixed_in_proxy'
        self.assertIsNone(gc.classification(c, groups(), ('site.example',)))

    def test_historical_bytes_do_not_prevent_quiet_collection(self):
        book = gc.TrafficBook(); c = conn()
        for t in (0, 8, 16):
            self.assertEqual(book.observe(groups(), [c], t), [])
        self.assertEqual(book.observe(groups(), [c], 24)[0][0]['id'], 'c')

    def test_active_transfer_resets_quiet_clock(self):
        book = gc.TrafficBook(); c = conn()
        for t in (0, 8, 16): book.observe(groups(), [c], t)
        c = copy.deepcopy(c); c['download'] += 4096
        self.assertEqual(book.observe(groups(), [c], 24), [])
        for t in (32, 40): self.assertEqual(book.observe(groups(), [c], t), [])
        self.assertEqual(len(book.observe(groups(), [c], 48)), 1)

    def test_keepalive_trickle_remains_quiet(self):
        book = gc.TrafficBook(); c = conn()
        for t in (0, 8, 16, 24):
            c = copy.deepcopy(c); c['upload'] += 32
            found = book.observe(groups(), [c], t)
        self.assertEqual(len(found), 1)

    def test_current_and_other_group_selected_protected(self):
        book = gc.TrafficBook(); gs = groups()
        gs['other'] = {'now': 'old', 'all': ['old'], 'type': 'URLTest'}
        for t in (0, 8, 16, 24, 32):
            self.assertEqual(book.observe(gs, [conn()], t), [])

    def test_current_node_never_collected(self):
        book = gc.TrafficBook()
        for t in (0, 8, 16, 24): self.assertEqual(book.observe(groups('old'), [conn()], t), [])

    def test_missing_or_empty_group_is_safe(self):
        for gs in ({}, groups(''), {'other': {'now': 'new'}}):
            book = gc.TrafficBook()
            for t in (0, 8, 16, 24): self.assertEqual(book.observe(gs, [conn()], t), [])

    def test_unknown_chain_skipped(self):
        c = conn(); c['chains'].append('unknown-selector')
        book = gc.TrafficBook()
        for t in (0, 8, 16, 24): self.assertEqual(book.observe(groups(), [c], t), [])

    def test_ssh_and_udp_protected(self):
        for c in (conn(port='22'), conn(port='2222'), conn(network='udp')):
            book = gc.TrafficBook()
            for t in (0, 8, 16, 24): self.assertEqual(book.observe(groups(), [c], t), [])

    def test_pause_or_sleep_requires_new_samples(self):
        book = gc.TrafficBook(); c = conn()
        for t in (0, 8, 16): book.observe(groups(), [c], t)
        self.assertEqual(book.observe(groups(), [c], 80), [])
        book.reset()
        self.assertEqual(book.observe(groups(), [c], 88), [])

    def test_counter_reset_requires_new_samples(self):
        book = gc.TrafficBook(); c = conn()
        for t in (0, 8, 16): book.observe(groups(), [c], t)
        c = copy.deepcopy(c); c['upload'] = 0
        self.assertEqual(book.observe(groups(), [c], 24), [])

    def test_budget_and_disappeared_connections(self):
        book = gc.TrafficBook(); cs = [conn(str(i)) for i in range(40)]
        for t in (0, 8, 16, 24): found = book.observe(groups(), cs, t)
        self.assertEqual(len(found), gc.CLOSE_BUDGET)
        book.observe(groups(), [], 32)
        self.assertEqual(book.samples, {})

    def test_direct_connections_never_deleted(self):
        c = conn(); c['chains'] = ['direct_out']
        book = gc.TrafficBook()
        for t in (0, 8, 16, 24): self.assertEqual(book.observe(groups(), [c], t, ('site.example',)), [])

    def test_misroute_suffix_boundary(self):
        c = conn(node='new')
        self.assertEqual(gc.classification(c, groups(), ('site.example',)), 'force-direct-misroute')
        c['metadata']['host'] = 'notsite.example'
        self.assertIsNone(gc.classification(c, groups(), ('site.example',)))

    def test_recheck_active_reselected_reused_id(self):
        c = conn(); current = copy.deepcopy(c)
        self.assertTrue(gc.still_eligible(c, current, 'quiet-old-node', groups(), ()))
        current['download'] += 1
        self.assertFalse(gc.still_eligible(c, current, 'quiet-old-node', groups(), ()))
        self.assertFalse(gc.still_eligible(c, c, 'quiet-old-node', groups('old'), ()))
        current = copy.deepcopy(c); current['start'] = '2020-01-01T00:00:00+00:00'
        self.assertFalse(gc.still_eligible(c, current, 'quiet-old-node', groups(), ()))
        self.assertFalse(gc.still_eligible(c, None, 'quiet-old-node', groups(), ()))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.patch = patch.object(km, 'STATE_DIR', self.path); self.patch.start(); self.addCleanup(self.patch.stop)
        km.STOP.clear()

    def test_single_instance(self):
        with km.InstanceLock('test'):
            with self.assertRaises(RuntimeError): km.InstanceLock('test')
        with km.InstanceLock('test'): pass

    def test_atomic_permissions(self):
        p = self.path / 'x.json'; km.atomic_json(p, {'x': 1})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(km.load_json(p), {'x': 1})

    def test_bounded_log_rotation(self):
        log = km.logger('test-rotation')
        handler = log.handlers[0]
        handler.maxBytes = 150
        for _ in range(20): log.info('x' * 100)
        handler.close(); log.handlers.clear()
        logs = list(self.path.glob('test-rotation.log*'))
        self.assertLessEqual(len(logs), 4)
        self.assertGreater(len(logs), 1)

    def test_json_shapes_fail_closed(self):
        for bad in ({}, {'connections': None}, {'connections': [{'id': 'c'}]}, {'connections': [conn(), conn()]}):
            with patch.object(km, 'api_json', return_value=bad):
                with self.assertRaises(ValueError): km.connections()

    def test_nested_selector_cycle(self):
        with self.assertRaises(ValueError): km.selected_nodes({'a': {'now': 'b'}, 'b': {'now': 'a'}})

    def test_group_reader_partial_api_failure(self):
        reader = km.GroupReader()
        with patch.object(km, 'api_json', side_effect=[{'proxies': groups()}, {}]):
            with self.assertRaises(ValueError): reader.read()

    def test_loopback_api_has_no_system_proxy_lookup(self):
        response = Mock(status=200); response.read.return_value = b'{"version":"test"}'
        http = Mock(); http.getresponse.return_value = response
        with patch.object(km, 'service_config', return_value={'control_port': 3057, 'secret': 'not-logged'}), patch.object(km.http.client, 'HTTPConnection', return_value=http) as factory:
            self.assertEqual(km.api_json('/version'), {'version': 'test'})
            factory.assert_called_once_with('127.0.0.1', 3057, timeout=4.0)
            http.close.assert_called_once()

    def collector(self, apply):
        with patch.object(km, 'logger', return_value=Mock()):
            return gc.Collector(apply)

    def test_dry_run_never_deletes(self):
        collector = self.collector(False); c = conn()
        collector.reader.read = Mock(return_value=groups())
        collector.book.observe = Mock(return_value=[(c, 'quiet-old-node')])
        with patch.object(km, 'connections', return_value=[c]), patch.object(gc, 'direct_suffixes', return_value=()), patch.object(km, 'api_request') as api:
            self.assertTrue(collector.step()); api.assert_not_called()
        self.assertFalse((self.path / 'connection-gc.json').exists())

    def test_delete_failure_not_counted(self):
        collector = self.collector(True); c = conn()
        collector.reader.read = Mock(return_value=groups())
        collector.book.observe = Mock(return_value=[(c, 'quiet-old-node')])
        with patch.object(km, 'connections', return_value=[c]), patch.object(gc, 'direct_suffixes', return_value=()), patch.object(km, 'api_request', side_effect=OSError('offline')):
            self.assertFalse(collector.step())
        self.assertEqual(collector.totals['delete_accepted'], 0)
        self.assertEqual(collector.totals['delete_failed'], 1)
        self.assertEqual(collector.book.samples, {})

    def test_apply_confirm_absence(self):
        collector = self.collector(True); c = conn()
        collector.reader.read = Mock(return_value=groups())
        collector.book.observe = Mock(return_value=[(c, 'quiet-old-node')])
        with patch.object(km, 'connections', side_effect=[[c], [c], []]), patch.object(gc, 'direct_suffixes', return_value=()), patch.object(km, 'api_request', return_value=(204, None)) as api:
            self.assertTrue(collector.step()); self.assertEqual(api.call_count, 1)
        self.assertEqual(collector.totals['delete_accepted'], 1)
        self.assertEqual(collector.totals['confirmed_absent'], 1)

    def test_api_failure_resets_sampling(self):
        collector = self.collector(True)
        collector.book.observe(groups(), [conn()], 0)
        collector.reader.read = Mock(side_effect=OSError('offline'))
        with patch.object(km, 'api_request') as api:
            self.assertFalse(collector.step()); api.assert_not_called()
        self.assertEqual(collector.book.samples, {})

    def test_health_hysteresis(self):
        health = watch.Health()
        self.assertIsNone(health.note(False)); self.assertIsNone(health.note(False))
        self.assertEqual(health.note(False), 'down'); self.assertIsNone(health.note(False))
        self.assertIsNone(health.note(True)); self.assertEqual(health.note(True), 'recovered')
        self.assertIsNone(health.note(True))

    def test_transient_failures_do_not_alert(self):
        health = watch.Health()
        for ok in (True, True, False, True, False, False, True, True):
            self.assertIsNone(health.note(ok))
        self.assertEqual(health.state, 'up')


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('KARING_DIR', self.root / 'karing'), ('STATE_DIR', self.root / 'state')]:
            value.mkdir(); p = patch.object(km, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(sync, 'BACKUP', self.root / 'state/backups'); p.start(); self.addCleanup(p.stop)
        self.routing = {'items': [{'groupid': 'other', 'groups': []}, {'groupid': 'custom', 'groups': [
            {'name': '🌏 国外穿墙', 'marker': 'keep'}, {'name': '🍎 苹果服务', 'domain_suffix': ['extra.apple.example'], 'custom_key': 7},
            {'name': '🏠 强制直连', 'domain_suffix': ['internal.example']}, {'name': '💬 OpenAI', 'domain_suffix': ['openai.com']}]}]}
        self.use = {'diversion_group': [{'diversion_name': '💬 OpenAI', 'diversion_groupid': 'custom', 'dns_servers': ['custom-dns'], 'server_name': 'wrong'}], 'private': 'keep'}
        for name, data in zip(sync.FILES, (self.routing, self.use)):
            km.atomic_json(km.KARING_DIR / name, data)
        km.atomic_json(km.KARING_DIR / 'service_core.json', {})

    def test_preserve_unmanaged_fields_and_merge_domains(self):
        r, u = sync.prepare(self.routing, self.use)
        groups = sync.groups_in(r)
        self.assertEqual(groups[0]['custom_key'], 7)
        self.assertIn('extra.apple.example', groups[0]['domain_suffix'])
        self.assertEqual(groups[-1]['marker'], 'keep')
        self.assertEqual(u['private'], 'keep')
        self.assertEqual(next(x for x in u['diversion_group'] if x['diversion_name'] == '💬 OpenAI')['dns_servers'], ['custom-dns'])

    def test_idempotent_apply_no_rewrite_backup(self):
        with patch.object(sync, 'direct_config', return_value=None):
            first = sync.apply(); self.assertTrue(first['changed'])
            stamps = {p.name: p.stat().st_mtime_ns for p in km.KARING_DIR.iterdir()}
            second = sync.apply(); self.assertEqual(second['changed'], [])
            self.assertEqual(stamps, {p.name: p.stat().st_mtime_ns for p in km.KARING_DIR.iterdir()})
            self.assertEqual(len(list(sync.BACKUP.iterdir())), 1)
            for name in (*sync.FILES, 'service_core.json'): self.assertTrue((Path(first['backup']) / name).exists())

    def test_restore_roundtrip(self):
        with patch.object(sync, 'direct_config', return_value=None): first = sync.apply()
        sync.restore(first['backup'])
        self.assertEqual(km.load_json(km.KARING_DIR / sync.FILES[0]), self.routing)
        self.assertEqual(km.load_json(km.KARING_DIR / sync.FILES[1]), self.use)

    def test_invalid_json_no_partial_write(self):
        before = (km.KARING_DIR / sync.FILES[0]).read_bytes()
        (km.KARING_DIR / sync.FILES[1]).write_text('{')
        with self.assertRaises(ValueError): sync.apply()
        self.assertEqual((km.KARING_DIR / sync.FILES[0]).read_bytes(), before)

    def test_failed_second_write_rolls_back_first(self):
        original = km.atomic_json
        def fail(path, data):
            if path.name == sync.FILES[1]: raise OSError('disk error')
            return original(path, data)
        with patch.object(sync, 'direct_config', return_value=None), patch.object(km, 'atomic_json', side_effect=fail):
            with self.assertRaises(OSError): sync.apply()
        self.assertEqual(km.load_json(km.KARING_DIR / sync.FILES[0]), self.routing)

    def test_duplicate_groups_rejected(self):
        self.routing['items'][1]['groups'].append({'name': '🍎 苹果服务'})
        with self.assertRaises(ValueError): sync.prepare(self.routing, self.use)

    def test_direct_domain_config_additive(self):
        r, _ = sync.prepare(self.routing, self.use, {'domain_suffix': ['second.internal.example']})
        group = next(g for g in sync.groups_in(r) if g['name'] == '🏠 强制直连')
        self.assertEqual(group['domain_suffix'], ['internal.example', 'second.internal.example'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
