import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tools import readiness
from tools.serve_result_site import StaticHandler

ROOT = Path(__file__).resolve().parents[1]


def stand(td):
    root = Path(td)
    shutil.copytree(ROOT / readiness.STARTER, root / readiness.STARTER)
    return root


class Readiness(unittest.TestCase):
    def test_steps_in_order_and_not_ready_without_calibration(self):
        with tempfile.TemporaryDirectory() as td:
            root = stand(td)
            state = readiness.snapshot(root)
            self.assertEqual([s['id'] for s in state['steps']],
                             ['github', 'platform', 'protection', 'agent', 'isolation', 'starter', 'calibration', 'harnesses'])
            self.assertFalse(state['ready'])
            self.assertEqual(next(s for s in state['steps'] if s['id'] == 'starter')['state'], 'ok')
            self.assertIn(next(s for s in state['steps'] if s['id'] == 'calibration')['state'], ('missing', 'blocked'))

    def test_only_discovered_programs_can_be_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = stand(td)
            with self.assertRaises(ValueError):
                readiness.act('select', {'platform': '/tmp/not-1cv8'}, root)
            with self.assertRaises(ValueError):
                readiness.act('select', {'path': '/bin/sh'}, root)
            self.assertFalse((root / '.local/community/settings.json').exists())

    def test_harness_cannot_be_prepared_before_calibration_or_without_a_package(self):
        with tempfile.TemporaryDirectory() as td:
            root = stand(td)
            items = next(s for s in readiness.snapshot(root)['steps'] if s['id'] == 'harnesses')['items']
            self.assertTrue(items and all(h['action'] is None for h in items))
            with patch('tools.community_service.start') as start:
                for data in ({'id': items[0]['id']}, {'id': 'missing'}):
                    with self.assertRaises(ValueError):
                        readiness.act('prepare-harness', data, root)
                start.assert_not_called()

    def test_models_are_offered_per_agent_from_the_shipped_list(self):
        self.assertTrue(readiness.model_allowed('codex', readiness.agents()['codex']['default_model']))
        self.assertTrue(readiness.model_allowed('claude', 'claude-opus-5-5'))
        self.assertFalse(readiness.model_allowed('codex', 'claude-opus-5-5'))
        self.assertFalse(readiness.model_allowed('codex', '--dangerously-bypass'))
        for agent in readiness.agents().values():
            self.assertIn(agent['default_model'], [m['id'] for m in agent['models']])

    @unittest.skipIf(os.name == 'nt', 'Builds POSIX paths; Windows cannot instantiate PosixPath')
    def test_version_comes_from_path_and_exam_platform_is_preferred(self):
        found = [Path('/opt/1cv8/8.5.4.1878/1cv8'), Path('/opt/1cv8/8.3.27.2074/1cv8')]
        with patch.object(Path, 'glob', lambda self, pattern: found if pattern == '*/1cv8' else []), \
             patch.object(Path, 'is_file', lambda self: True), patch.object(Path, 'resolve', lambda self: self), \
             patch('shutil.which', return_value=None), patch('os.name', 'posix'):
            result = readiness.discover_platforms()
        self.assertEqual([c['version'] for c in result], ['8.3.27.2074', '8.5.4.1878'])


class ReadinessHTTP(unittest.TestCase):
    def test_actions_need_token_and_local_origin(self):
        handler = type('ReadinessTestHandler', (StaticHandler,), {'controller': None, 'community_token': 'test-csrf', 'log_message': lambda *a: None})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/readiness/calibrate'
            with patch('tools.readiness.act') as act:
                for headers in ({}, {'X-Demo-Token': 'test-csrf', 'Origin': 'https://unrelated.example'}):
                    request = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json', **headers})
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request)
                    self.assertEqual(error.exception.code, 403)
                act.assert_not_called()
                act.return_value = {'started': True}
                request = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json', 'X-Demo-Token': 'test-csrf'})
                self.assertEqual(json.load(urllib.request.urlopen(request)), {'started': True})
                act.assert_called_once()
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main()


class ParticipantStand(unittest.TestCase):
    def test_site_starts_without_an_owner_baseline(self):
        from tools.serve_result_site import watch_signature
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('site', 'tasks', 'rules', 'starters', 'docs'):
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('dist', '.dist-staging', '*.dt'))
            readiness.write(root / '.local/demo/config.json', {'baseline_evaluation': '.local/community/no-baseline.json'})
            watch_signature(root, catalogue_only=True)
