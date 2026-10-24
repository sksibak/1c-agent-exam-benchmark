import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exam_bench import claude

ROOT = Path(__file__).resolve().parents[1]


class ClaudePortable(unittest.TestCase):
    def test_no_macos_paths_are_hardcoded(self):
        text = (ROOT / 'exam_bench/claude.py').read_text(encoding='utf-8')
        for fragment in ('/opt/homebrew', '/opt/1cv8', ".1cv82/1C/1Cv82'"):
            self.assertNotIn(fragment, text)

    def test_worker_environment_is_the_shared_allowlist_plus_claude_settings(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'NODE_EXTRA_CA_CERTS': '/certs/corp.pem', 'OPENAI_API_KEY': 'secret'}):
            ws = Path(td); runtime = ws / 'client-home'
            env = claude.worker_environment(ws, runtime, {'worker_env': {'EXTRA': '1'}})
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(runtime))
        self.assertEqual(env['NODE_EXTRA_CA_CERTS'], '/certs/corp.pem')  # TLS interception keeps working
        self.assertNotIn('OPENAI_API_KEY', env)  # host secrets do not leak into the worker
        self.assertEqual((env['TMP'], env['TEMP'], env['CLAUDE_CODE_TMPDIR']), (str(ws / 'tmp'),) * 3)
        self.assertEqual(env['EXTRA'], '1')

    def test_portable_harness_is_registered_for_claude_code(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td); runtime = ws / 'client-home'; runtime.mkdir()
            package = ws / 'harness/tool'
            def configure_portable(name, ws_, runtime_, run_):
                tree = runtime_ / 'skills' / name / 'review'; tree.mkdir(parents=True)
                (tree / 'SKILL.md').write_text('# review')
                return {'name': name, 'package_path': str(package)}, ''
            with patch('exam_bench.portable_harnesses.available', return_value=True), \
                 patch('exam_bench.portable_harnesses.configure', side_effect=configure_portable), \
                 patch('exam_bench.portable_harnesses.entry', return_value={'server': 'tool'}), \
                 patch('exam_bench.portable_harnesses.spec', return_value=('tool.exe', ['mcp'], {'A': 'b'})):
                harness, mcp = claude.configure('tool-1', ws, runtime, ws)
            self.assertTrue((runtime / 'skills/review/SKILL.md').is_file())  # one level deep, where Claude Code looks
            self.assertEqual(json.loads(mcp.read_text())['mcpServers']['tool'],
                             {'type': 'stdio', 'command': 'tool.exe', 'args': ['mcp'], 'env': {'A': 'b'}})


if __name__ == '__main__':
    unittest.main()
