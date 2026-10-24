"""Persistent local conference demo: isolated Codex attempt, then calibrated acceptance.

No HTTP-supplied paths, commands, credentials or model names reach this worker.
Only allowlisted public result fields are returned to the browser.
"""
import argparse
import collections
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ACTIVE = {'queued', 'preparing', 'solving', 'checking_gates', 'grading', 'publishing'}
TIME_LIMITS = (0.5, 1, 2, 3, 5)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def config(root=ROOT):
    return read(root / '.local/demo/config.json')


def local_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Demo configuration path escapes repository')
    return path


def prerequisites(root=ROOT, fresh=True):
    from exam_bench.common import digest, tree_manifest
    from exam_bench.preflight import verify_calibration
    from exam_bench.runtime import machine_identity, require_execution_mode
    cfg = config(root)
    os.environ['EXAM_BENCH_CONFIG'] = str(local_path(root, cfg['machine_config']))
    require_execution_mode()
    calibration_path = local_path(root, cfg['calibration'])
    calibration = read(calibration_path)
    verify_calibration(calibration)
    if calibration['machine'] != machine_identity():
        raise ValueError('Calibration does not match this machine')
    if not Path(machine_identity()['platform_binary']).is_file():
        raise ValueError('1C platform is missing')
    starter = local_path(root, cfg['starter_source'])
    if not (starter / 'Configuration.xml').is_file():
        raise ValueError('Verified starter source is missing')
    if tree_manifest(starter) != read(local_path(root, cfg['starter_source_manifest'])):
        raise ValueError('Verified starter source changed')
    if digest(local_path(root, cfg['starter_dt'])) != cfg['starter_dt_sha256']:
        raise ValueError('Starter DT changed')
    return cfg, calibration


def public_evaluation(root, path):
    """Keep raw logs, billing/account data and local paths off the website."""
    from exam_bench.common import digest, tree_manifest
    r = read(path)
    run = root / '.local/runs' / r['run_id']
    if tree_manifest(run / 'artifacts') != read(run / 'artifact-manifest.json'):
        raise ValueError('Frozen submission changed')
    if digest(run / 'artifact-manifest.json') != r['artifact_manifest_sha256']:
        raise ValueError('Assessment refers to a different submission')
    score = r['assessment']['score']
    failed = [x['id'] for x in r['rules'] if x['verdict'] == 'fail']
    reasons = {'OU20': 'Ведомость не прошла проверку группировки по значениям характеристик.'}
    result = read(run / 'result.json')
    return {'run_id': r['run_id'], 'created_at': r['created_at'],
            'model': result['model']['requested'], 'harness': result['harness'],
            'solve_seconds': round(result['time']['solve_ms'] / 1000),
            'platform': r['machine']['platform_version'],
            'counts': dict(collections.Counter(x['verdict'] for x in r['rules'])),
            'rules': [{'id': x['id'], 'verdict': x['verdict'], 'coverage': x.get('coverage'),
                       'reason': reasons.get(x['id']) if x['id'] in failed else None} for x in r['rules']],
            'charges': [{'id': x['penalty_id'], 'points': x['points'],
                         'reason': reasons.get(x['defect_id'].removeprefix('requirement.'), x['reason'])} for x in score['charges']],
            'score_upper_bound': score['score_upper_bound'], 'score': score['score'], 'grade': score['grade'],
            'deductions': score['deductions'], 'not_assessed': sum(x['status'] == 'not_assessed' for x in r['assessment']['statuses'].values()),
            'gates': {key: value.get('status', 'passed' if value.get('passed') else 'failed') for key, value in r['gates'].items()},
            'infrastructure_errors': bool(r['errors']), 'comparison_eligible': False,
            'binding_compatibility': r.get('binding_compatibility'),
            'evaluation_sha256': digest(path), 'downloads': [
                {'label': 'Решение · ZIP', 'url': f'downloads/{r["run_id"]}/solution.zip'},
                {'label': 'Контрольные суммы · JSON', 'url': f'downloads/{r["run_id"]}/manifest.json'}]
                if (root / 'site/dist/downloads' / r['run_id'] / 'solution.zip').is_file() else []}


def publication(root=ROOT):
    cfg = config(root)
    evaluations = [local_path(root, cfg['baseline_evaluation'])]
    evaluations += sorted((root / '.local/demo/jobs').glob('*/evaluation.json'))
    results = [public_evaluation(root, p) for p in evaluations if p.is_file()]
    results.sort(key=lambda x: x['created_at'], reverse=True)
    return {'enabled': True, 'model': cfg['model'], 'timeout_seconds': cfg['timeout_seconds'],
            'platform': cfg['platform_version'], 'results': results, 'harnesses': harness_options(root)}


def harness_options(root=ROOT):
    from exam_bench.common import digest
    records = root / '.local/harnesses/portable.json'
    result = [{'id': 'none', 'label': 'Без дополнительных харнесов', 'ready': True}]
    for name, value in (read(records) if records.is_file() else {}).items():
        receipt = root / '.local/harnesses' / name / 'capability.json'
        from exam_bench.portable_harnesses import profile as sealed_profile
        profile = sealed_profile(name, root)
        data = read(receipt) if receipt.is_file() else {}
        ready = bool(data.get('accepted') and profile.is_file() and data.get('profile_sha256') == digest(profile)
                     and read(profile).get('configuration') == value)
        result.append({'id': name, 'label': value['label'], 'ready': ready,
                       'note': data.get('scope') if ready else
                       'Нужно вручную разрешить открытие MCP_Toolkit.epf в тестовой 1С.' if name == 'mcp-toolkit-1.9.0' else
                       'Нативная проверка ещё не завершена или профиль изменился.'})
    return result


def history(root=ROOT):
    """Every local attempt, newest first, with its evaluation and submission state."""
    items = []
    for job in sorted((root / '.local/demo/jobs').glob('demo-*'), reverse=True):
        if not (job / 'status.json').is_file():
            continue
        status = read(job / 'status.json')
        mode = status.get('mode')
        if mode != 'run' and not (mode == 'regrade' and (job / 'evaluation.json').is_file()):
            continue
        item = {'job_id': job.name, 'mode': mode, 'phase': status['phase'], 'harness': status.get('harness', 'none'),
                'started_unix': status['started_unix'], 'error': status.get('error'), 'evaluation': None,
                'prepared': (root / '.local/community/prepared' / job.name / 'manifest.json').is_file(), 'pr_url': None}
        pr = root / '.local/community/pr' / (job.name + '.json')
        if pr.is_file():
            item['pr_url'] = read(pr)['url']
        if (job / 'evaluation.json').is_file():
            try:
                item['evaluation'] = public_evaluation(root, job / 'evaluation.json')
            except (OSError, KeyError, ValueError):
                item['error'] = 'Оценка не прошла проверку целостности.'
        item.update(run_summary(root, job, status))
        items.append(item)
    return items


def agent_log(root, job_id, limit=3000):
    """The agent's own log of a local attempt as readable steps: what it said, ran and got back.

    Only for this machine's attempts; raw logs never leave it.
    """
    if not job_id.startswith('demo-') or not all(c.isalnum() or c == '-' for c in job_id):
        raise ValueError('Invalid job ID')
    status = read(root / '.local/demo/jobs' / job_id / 'status.json')
    raw = root / '.local/runs' / status.get('run_id', job_id) / 'raw/stdout.jsonl'
    if not raw.is_file():
        raise ValueError('Лога этой попытки нет')
    cut = lambda text, n=4000: text if len(text) <= n else text[:n] + f'\n… ещё {len(text) - n} символов'
    steps = []
    for line in raw.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get('item') or {}
        if event.get('type') == 'item.completed':  # Codex
            if item.get('type') == 'agent_message':
                steps.append({'kind': 'message', 'text': cut(item.get('text', ''))})
            elif item.get('type') == 'command_execution':
                steps.append({'kind': 'command', 'text': item.get('command', ''), 'output': cut(item.get('aggregated_output') or ''),
                              'exit_code': item.get('exit_code')})
            elif item.get('type') == 'mcp_tool_call':
                steps.append({'kind': 'tool', 'text': f"{item.get('server', '')} · {item.get('tool', '')}",
                              'output': cut(json.dumps(item.get('result') or item.get('error'), ensure_ascii=False))})
        message = event.get('message')
        if isinstance(message, dict) and isinstance(message.get('content'), list):  # Claude Code
            for block in message['content']:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and event.get('type') == 'assistant':
                    steps.append({'kind': 'message', 'text': cut(block.get('text', ''))})
                elif block.get('type') == 'tool_use':
                    steps.append({'kind': 'tool', 'text': block.get('name', ''), 'output': cut(json.dumps(block.get('input'), ensure_ascii=False), 2000)})
                elif block.get('type') == 'tool_result':
                    content = block.get('content')
                    text = content if isinstance(content, str) else '\n'.join(c.get('text', '') for c in content or [] if isinstance(c, dict))
                    if steps and steps[-1]['kind'] == 'tool':
                        steps[-1]['result'] = cut(text)
        if len(steps) >= limit:
            steps.append({'kind': 'message', 'text': f'… лог обрезан на {limit} шагах'})
            break
    return {'job_id': job_id, 'steps': steps}


def run_summary(root, job, status):
    """What the leaderboard row shows besides the score: agent, limits, tokens, machine, author."""
    run = root / '.local/runs' / status.get('run_id', job.name)
    plan = read(run / 'plan.json') if (run / 'plan.json').is_file() else {}
    result = read(run / 'result.json') if (run / 'result.json').is_file() else {}
    measurement = read(job / 'measurement.json') if (job / 'measurement.json').is_file() else {}
    tokens = result.get('metrics', {}).get('tokens', {})
    environment = measurement.get('environment', {})
    return {'run': {'client': plan.get('client'), 'client_version': plan.get('client_version'), 'model': plan.get('model'),
                    'reasoning_effort': plan.get('reasoning_effort'), 'solve_timeout_seconds': plan.get('solve_timeout_seconds'),
                    'tokens': {k: tokens.get(k) for k in ('input', 'cached_input', 'fresh_input', 'output')},
                    'tools': {k: result.get('metrics', {}).get('tools', {}).get(k) for k in ('mcp_calls', 'failed_calls')}},
            'measurement': {'operator': measurement.get('operator'),
                            'environment': {k: environment.get(k) for k in ('os', 'hardware', 'platform_version', 'isolation')}}}


def export(root, stage):
    if not (root / '.local/demo/config.json').is_file():
        return
    from tools.build_result_site import package_solution
    data = publication(root)
    for result in data['results']:
        result['downloads'] = package_solution(root / '.local/runs' / result['run_id'], stage)
    atomic(stage / 'demo.json', data)


class Controller:
    def __init__(self, root=ROOT):
        self.root = root
        self.lock = threading.Lock()
        self.process = None
        self.token = secrets.token_urlsafe(32)

    def snapshot(self):
        cfg = config(self.root)
        pointer = self.root / '.local/demo/current.json'
        state = read(pointer) if pointer.is_file() else None
        if state:
            job = self.root / '.local/demo/jobs' / state['job_id']
            state = read(job / 'status.json')
            state['elapsed_seconds'] = round(state.get('ended_unix', time.time()) - state['started_unix'])
            if state['phase'] in ACTIVE:
                if self.process and self.process.pid == state.get('pid') and self.process.poll() is not None:
                    state.update(phase='failed', error='Рабочий процесс завершился. Подробности в локальном журнале.')
                    atomic(job / 'status.json', state)
                elif not alive(state.get('pid')) and time.time() - state['started_unix'] > 10:
                    state.update(phase='failed', error='Рабочий процесс прерван; сохранённые результаты доступны ниже.')
                    atomic(job / 'status.json', state)
                state['elapsed_seconds'] = round(time.time() - state['started_unix'])
                run = self.root / '.local/runs' / state['run_id']
                raw = run / 'raw/stdout.jsonl'
                if state['phase'] == 'preparing' and raw.exists():
                    state['phase'] = 'solving'
                state['activity_bytes'] = raw.stat().st_size if raw.exists() else 0
                state['cases_completed'] = len(list((job / 'grade').glob('*/observations.json')))
        try:
            prerequisites(self.root)
            ready, error = True, None
        except Exception as problem:
            messages = {'Codex CLI is missing': 'Codex CLI не найден. Проверьте путь после обновления приложения.',
                        'Codex login is missing': 'Нет входа в Codex. Выполните codex login.',
                        'FR-05 calibration absent or stale': 'Грейдер не проверен или обновился. Пройдите «Проверку грейдера» на странице «Замер».',
                        'Calibration does not match this machine': 'Грейдер проверялся для другой платформы или машины. Проверьте его заново.',
                        '1C platform is missing': 'Не найдена установленная платформа 1С. Проверьте путь в настройках.',
                        'Verified starter source is missing': 'Нет подготовленной каркасной конфигурации. Подготовьте стенд на странице участия.',
                        'Verified starter source changed': 'Контрольные суммы каркасной конфигурации изменились.',
                        'Starter DT changed': 'Контрольная сумма каркасной DT изменилась.'}
            ready, error = False, messages.get(str(problem), 'Не удалось проверить настройки стенда. Подробности доступны в локальном журнале.')
        if state:
            state = {key: state[key] for key in ('job_id','run_id','mode','harness','agent','model','phase','started_unix','ended_unix','timeout_seconds',
                     'completed_at','elapsed_seconds','activity_bytes','cases_completed','error','process_status') if key in state}
        return {'enabled': True, 'ready': ready, 'readiness_error': error, 'csrf': self.token,
                'operator_required': not (self.root / '.local/community/operator.json').is_file() or not read(self.root / '.local/community/operator.json').get('github'),
                'model': cfg['model'], 'platform': cfg['platform_version'], 'timeout_seconds': cfg['timeout_seconds'],
                'regrade_available': bool(cfg.get('baseline_run_id')),
                'job': state, 'results': publication(self.root)['results'], 'harnesses': harness_options(self.root)}

    def start(self, mode, harness='none', hours=None, model=None):
        from exam_bench.locking import file_lock
        from tools.readiness import agent_client, agents, model_allowed
        if mode not in ('run', 'regrade'):
            raise ValueError('Unknown demo action')
        with self.lock, file_lock(self.root / '.local/demo/start.lock'):
            # A running attempt is reported first: nothing else matters until it ends.
            state = self.snapshot()['job']
            if state and state['phase'] in ACTIVE:
                raise RuntimeError('Прогон уже выполняется')
            # The agent, its executable and credential come from discovery on this machine, never from HTTP.
            agent = agent_client(self.root) if mode == 'run' else ('codex', None, None)
            if agent is None:
                raise ValueError('Агент не готов: завершите шаг «Агент» на странице «Замер»')
            if mode == 'run':
                model = model or agents()[agent[0]]['default_model']
                if not model_allowed(agent[0], model):
                    raise ValueError('Эта модель не предлагается для выбранного агента')
            cfg, _ = prerequisites(self.root, fresh=mode == 'run')
            if mode == 'regrade':
                harness = 'none'
            if harness not in {x['id'] for x in harness_options(self.root) if x['ready']}:
                raise ValueError('Harness has not passed a capability probe')
            if mode == 'run':
                author = self.root / '.local/community/operator.json'
                if not author.is_file() or not read(author).get('github'):
                    raise ValueError('Sign in to GitHub before starting a measurement')
            if hours is not None and (isinstance(hours, bool) or hours not in TIME_LIMITS):
                raise ValueError('Unsupported time limit')
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            job_id = 'demo-' + stamp + '-' + secrets.token_hex(3)
            run_id = job_id if mode == 'run' else cfg['baseline_run_id']
            job = self.root / '.local/demo/jobs' / job_id
            job.mkdir(parents=True, exist_ok=False)
            if mode == 'run':
                from tools.community_results import record_measurement
                record_measurement(job, cfg, self.root)
            state = {'job_id': job_id, 'run_id': run_id, 'mode': mode, 'harness': harness, 'phase': 'queued',
                     'started_unix': time.time(), 'pid': None,
                     'timeout_seconds': int(hours * 3600) if hours else cfg['timeout_seconds'],
                     'agent': agent[0], 'client': agent[1], 'auth': str(agent[2]) if agent[2] else None, 'model': model}
            atomic(job / 'status.json', state)
            atomic(self.root / '.local/demo/current.json', {'job_id': job_id})
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
            try:
                with (job / 'stdout.log').open('wb') as out, (job / 'stderr.log').open('wb') as err:
                    self.process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'tools.demo_runner', '--job', job_id],
                        cwd=self.root, stdout=out, stderr=err, **options)
            except BaseException:
                state.update(phase='failed', error='Не удалось запустить рабочий процесс.')
                atomic(job / 'status.json', state)
                raise
            return {'job_id': job_id, 'run_id': run_id}


def alive(pid):
    if not pid:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def work(job_id, root=ROOT):
    if not job_id.startswith('demo-') or not all(c.isalnum() or c == '-' for c in job_id):
        raise ValueError('Invalid job ID')
    from exam_bench.common import digest, tree_manifest, utc, grading_fingerprint, write_json
    from exam_bench.runtime import machine_identity, platform_binary
    job = root / '.local/demo/jobs' / job_id
    state = read(job / 'status.json')

    def phase(name, **values):
        state.update(phase=name, pid=os.getpid(), **values)
        if name not in ACTIVE:
            state['ended_unix'] = time.time()
        atomic(job / 'status.json', state)

    try:
        phase('preparing')
        cfg, calibration = prerequisites(root, fresh=state['mode'] == 'run')
        calibration_path = local_path(root, cfg['calibration'])
        calibration_hash = digest(calibration_path)
        fingerprint = grading_fingerprint()
        dt = local_path(root, cfg['starter_dt'])
        task = root / 'tasks/1c-platform-official-sample'
        run = root / '.local/runs' / state['run_id']
        if state['mode'] == 'run':
            from exam_bench.harness import profile_path
            agent = state.get('agent', 'codex')
            client = state.get('client') or cfg['client']
            if agent == 'claude':
                from exam_bench.claude import start, client_version
                version, model = client_version(client), state.get('model') or 'claude-opus-5-5'
            else:
                from exam_bench.codex import start
                version = subprocess.run([client, '--version'], capture_output=True, text=True, check=True, timeout=15).stdout.strip().removeprefix('codex-cli ')
                model = state.get('model') or cfg['model']
            from exam_bench.materials import delivery_metadata
            selected_harness = state.get('harness', 'none')
            timeout = state.get('timeout_seconds', cfg['timeout_seconds'])
            if selected_harness != 'none':
                from exam_bench.harness import verify_package
                verify_package(selected_harness)
            policy = root / 'profiles/ai-exam-execution-policy.json'
            plan = {'schema_version': 1, 'run_id': state['run_id'], 'client': agent,
                'client_version': version, 'model': model,
                'reasoning_effort': 'medium', 'billing_channel': 'subscription', 'harness': selected_harness,
                'solve_timeout_seconds': timeout, 'stop_grace_seconds': 3,
                'deadline_disclosed': True, 'purpose': 'local_conference_demo', 'acceptance_profile': calibration['profile_id'],
                'delegation': 'native_disabled', 'network_profile': 'open-book_shell', 'machine': machine_identity(),
                'execution_policy_id': read(policy)['policy_id'], 'execution_policy_sha256': digest(policy),
                'statement_sha256': digest(task / 'public/statement.docx'), 'starter_dt_sha256': digest(dt),
                'profile_sha256': digest(profile_path(selected_harness)), 'public_inputs': tree_manifest(task / 'public'),
                'implementation_files': {'exam_bench/' + p.name: digest(p) for p in (root / 'exam_bench').glob('*.py')},
                'calibration_sha256': calibration_hash, **delivery_metadata(task)}
            series = next((name for name, item in read(policy).get('series', {}).items()
                           if item['solve_timeout_seconds'] == timeout), None)
            if series:
                plan['time_series'] = series
            write_json(job / 'plan.json', plan)
            result = start(plan, run, local_path(root, cfg['starter_source']), task, client,
                           Path(state.get('auth') or Path.home() / '.codex/auth.json'), starter_dt=dt)
            if not result['process']['stop_confirmed']:
                raise RuntimeError('Solver descendants did not stop')
            if not result['submission']['valid']:
                phase('invalid_submission', error='Codex завершил попытку без корректной сдачи. Оценка не выставлена.',
                      process_status=result['process']['status'])
                return
        art = run / 'artifacts'
        manifest = read(run / 'artifact-manifest.json')
        if tree_manifest(art) != manifest:
            raise ValueError('Frozen artifacts changed before acceptance')
        from exam_bench.submission import strip_extras, canonical_ledger, check_bindings
        from exam_bench.gates import check_all, starter_for
        from exam_bench.grader import grade, PROFILE
        from exam_bench.penalties import assess
        binding_file = job / 'binding-adapter.json' if state.get('binding_adapter') else art / 'binding.json'
        binding, _ = strip_extras(read(binding_file))
        binding, _ = canonical_ledger(art / 'src', binding)
        check_bindings(art / 'src', binding)
        phase('checking_gates')
        # Gate observers also export sources; keep their native paths below
        # Windows MAX_PATH, independently of the persistent job identifier.
        with tempfile.TemporaryDirectory(prefix='ed-') as temporary:
            work = Path(temporary)
            try:
                gates, ticket, may_grade = check_all(art / 'src', binding, dt, platform_binary(), work)
            finally:
                with zipfile.ZipFile(job / 'gate-evidence.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                    for path in work.rglob('*'):
                        relative = path.relative_to(work)
                        if path.is_file() and 'ib' not in relative.parts:
                            archive.write(path, relative.as_posix())
        write_json(job / 'gates.json', {'gates': gates, 'ticket': ticket, 'may_grade': may_grade})
        if not may_grade:
            phase('rejected', error='Сдача не прошла обязательные условия загрузки или происхождения.', gates=gates)
            return
        phase('grading')
        g = grade(art / 'src', binding, dt, platform_binary(), job / 'grade')
        assessment = assess(g['rules'], art / 'src', binding, read(task / 'checks.json')['requirements'], starter_for(dt),
                            read(task / 'private/official-assessment-mapping.json'), read(task / 'private/owner-scoring-policy.json'), gates, g['observations'])
        if fingerprint != grading_fingerprint() or calibration_hash != digest(calibration_path):
            raise ValueError('Grader or calibration changed during attempt')
        if tree_manifest(art) != manifest:
            raise ValueError('Frozen artifacts changed during acceptance')
        record = {'schema_version': 1, 'run_id': state['run_id'], 'evaluation_id': job_id, 'created_at': utc(),
                  'grading_fingerprint': fingerprint, 'calibration_ref': str(calibration_path), 'calibration_sha256': calibration_hash,
                  'artifact_manifest_sha256': digest(run / 'artifact-manifest.json'), 'profile': PROFILE['id'],
                  'gates': gates, 'status': g['status'], 'rules': g['rules'], 'errors': g['errors'], 'assessment': assessment,
                  'machine': machine_identity(), 'comparison_eligible': False}
        if state.get('binding_adapter'):
            record['binding_compatibility'] = read(job / 'binding-adapter-proof.json')
        write_json(job / 'evaluation.json', record)
        phase('publishing')
        # The server's watcher exports this immutable evaluation; HTTP can show
        # the result immediately even while the static export is being rebuilt.
        phase('finished', completed_at=utc(), evaluation_sha256=digest(job / 'evaluation.json'))
    except BaseException:
        traceback.print_exc()
        phase('failed', error='Прогон остановлен из-за ошибки стенда. Подробности сохранены в локальном журнале задания.')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    work(parser.parse_args().job)
