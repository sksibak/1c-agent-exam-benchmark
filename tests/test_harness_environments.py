import json
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from exam_bench.codex import worker_environment
from exam_bench.common import digest, tree_manifest, write_json
from exam_bench.harness import verify_package
from exam_bench.isolation import wrapped


class PinnedHarnessTest(unittest.TestCase):
    def test_recomputed_manifest_cannot_replace_the_pinned_package(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); package=root/'.local/harnesses/unica-0.12.3/package'
            (package/'.codex-plugin').mkdir(parents=True)
            write_json(package/'.codex-plugin/plugin.json',{'version':'0.12.3'})
            manifest=package.parent/'package-manifest.json'
            write_json(manifest,tree_manifest(package))
            profile=root/'profiles/unica-0.12.3-macos.json'
            write_json(profile,{'package_manifest_sha256':digest(manifest)})
            with patch('exam_bench.harness.ROOT',root):
                self.assertEqual(verify_package('unica-0.12.3'),package.parent)
                (package/'unexpected.txt').write_text('changed')
                with self.assertRaisesRegex(ValueError,'package changed'):
                    verify_package('unica-0.12.3')
                write_json(manifest,tree_manifest(package))
                with self.assertRaisesRegex(ValueError,'manifest differs'):
                    verify_package('unica-0.12.3')

    def test_requested_unpublished_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'Unsupported harness'):
            verify_package('unica-0.12.4')


@unittest.skipUnless(platform.system()=='Darwin','Verified macOS sandbox only')
class NativeShellTemporaryFilesTest(unittest.TestCase):
    def test_zsh_heredoc_and_python_import_under_worker_sandbox(self):
        with tempfile.TemporaryDirectory(prefix='exam-shell-test-') as td:
            ws=Path(td).resolve();(ws/'tmp').mkdir();runtime=ws/'client-home';runtime.mkdir()
            deps=runtime/'python-site-packages';deps.mkdir()
            (deps/'benchmark_dependency.py').write_text('VERSION="isolated"\n')
            with patch.dict('os.environ',{'BENCHMARK_MUST_NOT_LEAK':'secret'}):
                env=worker_environment(ws,runtime,{'worker_env':{
                    'PYTHONPATH':str(deps),'PYTHONDONTWRITEBYTECODE':'1'}})
            self.assertNotIn('BENCHMARK_MUST_NOT_LEAK',env)
            import sys,shlex
            command=shlex.quote(sys.executable)+" - <<'EOF'\nimport benchmark_dependency\nprint(benchmark_dependency.VERSION)\nEOF"
            p=subprocess.run(wrapped(['/bin/zsh','-lc',command],ws),cwd=ws,
                             env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(p.stdout,'isolated\n')
            self.assertEqual(list(deps.glob('__pycache__')),[])


class MCPEnvelopeTest(unittest.TestCase):
    def test_legacy_json_text_is_accepted_without_structured_content(self):
        from exam_bench.harness import mcp_call_succeeded
        base={'status':'completed','error':None,'result':{
            'structured_content':None,'content':[{'type':'text','text':'{"ok": true}'}]}}
        self.assertTrue(mcp_call_succeeded(base))
        base['result']['structured_content']={'ok':False}
        self.assertFalse(mcp_call_succeeded(base))
        base['result']['structured_content']=None
        base['result']['content'][0]['text']='ok: true'
        self.assertFalse(mcp_call_succeeded(base))
        base['result']['content'][0]['text']='{"ok": true}'
        base['error']={'message':'failed transport'}
        self.assertFalse(mcp_call_succeeded(base))


class RuntimeConfigurationVersionTest(unittest.TestCase):
    def test_old_runtime_gets_its_own_connection_schema_and_builder(self):
        from exam_bench.codex import project_configuration
        workspace=Path('/private/tmp/benchmark-test')
        old_main,old_local=project_configuration('unica-0.12.3',workspace,True)
        self.assertIn('builder: DESIGNER',old_main)
        self.assertIn('infobase:\n  connection:',old_local)
        self.assertNotIn('infobases:',old_local)
        self.assertIn('strict: true',old_local)
        new_main,new_local=project_configuration('unica-0.13.0',workspace,True)
        self.assertNotIn('builder:',new_main)
        self.assertIn('infobases:\n  origin:',new_local)
        self.assertIsNone(project_configuration('none',workspace,False)[1])


class RuntimeCapabilityGateTest(unittest.TestCase):
    def test_dry_run_and_mutated_receipt_cannot_pass_runtime_gate(self):
        from exam_bench.preflight import verify_runtime_capability
        from exam_bench.codex import project_configuration_fingerprint
        from exam_bench.harness import ROOT,profile_path
        (ROOT/'.local').mkdir(exist_ok=True)  # a fresh clone has no .local yet
        with tempfile.TemporaryDirectory(prefix='runtime-gate-',dir=ROOT/'.local') as td:
            directory=Path(td);evidence=directory/'evidence.json';receipt=directory/'capability.json'
            write_json(evidence,{'reply':{'ok':True,'cache':{'mode':'applied','events':['BuildCompleted']}}})
            data={'accepted':True,'profile_sha256':digest(profile_path('unica-0.12.3')),
                  'project_configuration_sha256':project_configuration_fingerprint('unica-0.12.3'),
                  'evidence_ref':'evidence.json','evidence_sha256':digest(evidence)}
            write_json(receipt,data)
            plan={'harness':'unica-0.12.3','profile_sha256':data['profile_sha256'],
                  'runtime_probe_ref':str(receipt.relative_to(ROOT))}
            verify_runtime_capability(plan)
            write_json(evidence,{'reply':{'ok':True,'cache':{'mode':'read','events':[]}}})
            with self.assertRaisesRegex(ValueError,'evidence changed'):verify_runtime_capability(plan)
            data['evidence_sha256']=digest(evidence);write_json(receipt,data)
            with self.assertRaisesRegex(ValueError,'execution not proved'):verify_runtime_capability(plan)
