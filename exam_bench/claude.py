"""Claude Code client adapter: the same worker, sandbox, inputs and freezing as the Codex adapter; its own launch,
harness registration, credentials and telemetry. No implicit retry, fallback or delegate.

Isolation: CLAUDE_CONFIG_DIR is an empty directory inside the worker (no user settings, memory, CLAUDE.md, plugins);
--setting-sources user reads only it. Web search and fetch are denied (the shell keeps network, as for Codex:
open-book shell); subagents are denied (native delegation disabled). Credentials: a subscription OAuth token made by
the owner with «claude setup-token», read from a file and passed as CLAUDE_CODE_OAUTH_TOKEN; like the Codex auth file,
it is readable by the solver's shell (known gap, recorded in client-profile.json).
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .common import digest, tree_manifest, utc, write_json
from .isolation import wrapped, verify
from .process import supervise
from .submission import extra_sections, freeze, validate, UnsupportedBindings
from .harness import verify_package, stop_owned_runtimes, is_unica
from .materials import solver_prompt, verify_delivery_plan
from .mcp_toolkit import NAME as TOOLKIT
from .stdio_harnesses import is_stdio_harness
from .runtime import platform_binary, cache_directory, child_environment, machine_identity, require_execution_mode, settings

DENIED_TOOLS = ('WebSearch', 'WebFetch', 'Task', 'Agent')
SETTINGS = {'permissions': {'defaultMode': 'bypassPermissions', 'deny': list(DENIED_TOOLS)},
            'autoMemoryEnabled': False, 'includeCoAuthoredBy': False, 'cleanupPeriodDays': 1}


def client_version(client):
    import subprocess
    out = subprocess.run([str(client), '--version'], capture_output=True, text=True, check=True).stdout.strip()
    if not out.endswith('(Claude Code)'):
        raise ValueError('Unsupported native client: ' + out)
    return out.split()[0]


def configure(name, ws, runtime, run):
    """Harness registration for Claude Code: the same pinned packages as for Codex.

    Unica: the published MCP server with the plugin's environment and the exact skills in the isolated user skill root.
    Shirokov: the same author Python port as for Codex (the pinned Claude package is PowerShell, absent on the worker);
    SKILL.md is a format shared by both clients; the skills keep their relative .codex/skills/... commands."""
    skills_root = runtime / 'skills'
    skills_root.mkdir(exist_ok=True)
    if name == 'none':
        return {'name': 'none', 'mcp_servers': [], 'additional_skills': []}, None
    from .portable_harnesses import available, configure as configure_portable, entry, spec
    if available(name) and name != TOOLKIT:
        # The same pinned package as for Codex, registered the Claude Code way.
        harness, _ = configure_portable(name, ws, runtime, run)
        # Claude Code discovers skills one level deep: copy each skill next to the author tree,
        # so relative references to sibling folders (../references) keep working.
        tree = skills_root / name
        for skill in sorted(tree.glob('*/SKILL.md')) if tree.is_dir() else []:
            if not (skills_root / skill.parent.name).exists():
                shutil.copytree(skill.parent, skills_root / skill.parent.name)
        value = entry(name)
        if not value.get('server'):
            return harness, None
        command, args, env = spec(name, ws, runtime, Path(harness['package_path']))
        write_json(runtime / 'mcp.json', {'mcpServers': {value['server']: {'type': 'stdio', 'command': command, 'args': args, 'env': env}}})
        return harness, runtime / 'mcp.json'
    if is_stdio_harness(name):
        from .stdio_harnesses import configure as configure_stdio
        return configure_stdio(name, ws, runtime, run, 'claude')
    if name == TOOLKIT:
        from .mcp_toolkit import configure as configure_toolkit
        return configure_toolkit(ws, runtime, run, 'claude')
    if name == 'shirokov-skills':
        source = verify_package('shirokov-skills')
        package = source / 'codex-python-package'
        shutil.copytree(package / '.codex', ws / '.codex')
        shutil.copytree(source / 'node-modules', ws / '.codex/skills/web-test/scripts/node_modules')
        for skill in sorted((ws / '.codex/skills').glob('*/SKILL.md')):
            (skills_root / skill.parent.name).symlink_to(skill.parent, target_is_directory=True)
        shutil.copytree(source / 'python-site-packages', runtime / 'python-site-packages')
        binary = runtime / 'bin'
        binary.mkdir()
        for exe in ('python', 'python3'):
            path = binary / exe
            path.write_text('#!/bin/sh\nexec ' + json.dumps(sys.executable) + ' "$@"\n', encoding='utf-8')
            path.chmod(0o755)
        if (ws / 'ib').exists():
            write_json(ws / '.v8-project.json', {'v8path': platform_binary(False),
                       'databases': [{'id': 'origin', 'name': 'Benchmark worker', 'type': 'file', 'path': str(ws / 'ib'),
                                      'aliases': ['origin', 'dev'], 'configSrc': str(ws / 'out/src')}]})
        write_json(run / 'shirokov-package-manifest.json', tree_manifest(ws / '.codex'))
        skills = sorted(p.parent.name for p in (ws / '.codex/skills').glob('*/SKILL.md'))
        return {'name': 'shirokov-skills', 'registration': 'author Python port (same as Codex); exact skills in the isolated Claude user skill root',
                'mcp_servers': [], 'additional_skills': skills, 'source_commit': json.loads((source / 'provenance.json').read_text(encoding='utf-8'))['commit'],
                'worker_env': {'PATH': os.pathsep.join([str(binary), str(Path(sys.executable).parent), os.environ.get('PATH', '')]),
                               'PYTHONPATH': str(runtime / 'python-site-packages'), 'PYTHONDONTWRITEBYTECODE': '1',
                               'PLAYWRIGHT_BROWSERS_PATH': str(ws / '.local/playwright')}}, None
    if not is_unica(name):
        raise ValueError('Unsupported harness: ' + name)
    source = verify_package(name)
    package = ws / 'harness/unica'
    shutil.copytree(source / 'package', package)
    shutil.copytree(source / 'client-home/unica', runtime / 'unica', ignore=shutil.ignore_patterns('.transactions'))
    shutil.copytree(package / 'skills', skills_root / 'unica')
    for directory in ('references', 'third-party'):
        shutil.copytree(package / directory, skills_root / directory)
    skills = sorted(p.parent.name for p in (package / 'skills').glob('*/SKILL.md'))
    # Skills are discovered one level deep: expose each Unica skill directly in the user skill root.
    for skill in skills:
        (skills_root / skill).symlink_to(skills_root / 'unica' / skill, target_is_directory=True)
    state = runtime / 'unica/provider-state'
    state.mkdir(parents=True, exist_ok=True)
    spec = json.loads((package / '.mcp.json').read_text(encoding='utf-8'))['mcpServers']['unica']
    env = {'CLAUDE_PLUGIN_ROOT': str(package), 'CLAUDE_PROJECT_DIR': str(ws), 'UNICA_HOST_CONTEXT_REQUIRED': '1',
           'UNICA_RUNTIME_CACHE_DIR': str(runtime / 'unica/runtimes'), 'UNICA_PROVIDER_STATE_DIR': str(state),
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null', 'TMPDIR': str(ws / 'tmp'),
           'UNICA_DAEMON_IDLE_GRACE_MS': '1000'}
    args = [a.replace('${CLAUDE_PLUGIN_ROOT}', str(package)) for a in spec['args']]
    command = spec['command'].replace('${CLAUDE_PLUGIN_ROOT}', str(package))
    write_json(runtime / 'mcp.json', {'mcpServers': {'unica': {'type': 'stdio', 'command': command, 'args': args, 'env': env}}})
    write_json(run / 'unica-mcp-registration.json', {'command': command, 'args': args, 'cwd': str(ws), 'env': env})
    write_json(run / 'unica-package-manifest.json', tree_manifest(package))
    return {'name': name, 'version': name.removeprefix('unica-'),
            'registration': 'published MCP (strict MCP config) plus exact skills in the isolated Claude user skill root; plugin loader not used',
            'mcp_servers': ['unica'], 'additional_skills': skills, 'runtime_manifest_sha256': digest(package / 'runtime-manifest.json')}, runtime / 'mcp.json'


def worker_environment(ws, runtime, harness):
    """The host allowlist shared with Codex plus what Claude Code itself needs on each OS."""
    env = child_environment(ws)
    env.update({'CLAUDE_CODE_TMPDIR': str(ws / 'tmp'), 'CLAUDE_CONFIG_DIR': str(runtime), 'DISABLE_AUTOUPDATER': '1',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1'})
    # A path to extra CA certificates (TLS interception by an antivirus or proxy); not a secret.
    if os.environ.get('NODE_EXTRA_CA_CERTS'):
        env['NODE_EXTRA_CA_CERTS'] = os.environ['NODE_EXTRA_CA_CERTS']
    if os.name == 'nt':
        # Claude Code on Windows runs its shell tool through Git Bash.
        bash = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH') or next(
            (str(p) for p in ([Path(shutil.which('git')).resolve().parents[1] / 'bin/bash.exe'] if shutil.which('git') else [])
             + [Path(os.environ.get(k, ''), 'Git/bin/bash.exe') for k in ('ProgramFiles', 'ProgramW6432')] if p.is_file()), None)
        if bash:
            env['CLAUDE_CODE_GIT_BASH_PATH'] = bash
    env.update(harness.get('worker_env', {}))
    if 'HOME' in os.environ:
        env['HOME'] = os.environ['HOME']
    return env


def prepare(plan, run, starter, task, client, auth_seed, starter_dt=None):
    from .preflight import validate_plan
    from .codex import bootstrap_lock, worker_document
    validate_plan(plan)
    if Path(run).exists():
        raise FileExistsError(run)
    require_execution_mode()
    if plan.get('machine') is not None and plan['machine'] != machine_identity():
        raise ValueError('Machine settings differ from the pinned plan')
    from .portable_harnesses import available as portable_harness
    if settings()['isolation'] != 'macos' and plan['harness'] != 'none' and not portable_harness(plan['harness']):
        raise ValueError('Outside macOS only harnesses prepared from harnesses/ are supported')
    run = Path(run).resolve()
    run.mkdir(parents=True, exist_ok=False)
    verify_delivery_plan(plan, task)
    implementation = run / 'implementation'
    implementation.mkdir()
    for source in Path(__file__).parent.glob('*.py'):
        shutil.copyfile(source, implementation / source.name)
    write_json(run / 'implementation-manifest.json', tree_manifest(implementation))
    ws = Path(tempfile.mkdtemp(prefix='1c-exam-worker-')).resolve()
    write_json(run / 'workspace.json', {'path': str(ws)})
    (ws / 'tmp').mkdir()
    (ws / 'out').mkdir()
    cache_paths = []
    if starter_dt is not None:
        from .platform import OneC
        cache_parent = cache_directory()
        cache_parent.mkdir(parents=True, exist_ok=True)
        lock_path = cache_parent / '.exam-benchmark-bootstrap.lock'
        with bootstrap_lock(lock_path):
            before = set(cache_parent.iterdir())
            adapter = OneC(platform_binary(), run / 'worker-bootstrap', 90)
            adapter.restore(ws / 'ib', starter_dt)
            adapter.export(ws / 'ib', ws / 'bootstrap-export')
            cache_paths = [str(p.resolve()) for p in set(cache_parent.iterdir()) - before if p.is_dir()]
            write_json(run / 'worker-bootstrap/cache-allocation.json', {'before': [str(p) for p in sorted(before)],
                       'new_directories': cache_paths, 'shared_host_lock': str(lock_path)})
            if settings()['isolation'] != 'dedicated-host' and len(cache_paths) != 1:
                raise ValueError('Cannot uniquely identify the new worker platform cache namespace')
        write_json(ws / 'platform-cache-grant.json', {'paths': cache_paths, 'owner': 'new benchmark worker only'})
        shutil.rmtree(ws / 'bootstrap-export')
    shutil.copytree(starter, ws / 'starter')
    if starter_dt is not None:
        shutil.copyfile(starter_dt, ws / 'starter.dt')
    shutil.copytree(starter, ws / 'out/src')
    shutil.copytree(task / 'public', ws / 'task')
    runtime = ws / 'client-home'
    runtime.mkdir(mode=0o700)
    write_json(runtime / 'settings.json', SETTINGS)
    harness, mcp_config = configure(plan['harness'], ws, runtime, run)
    leftovers = [p.name for p in runtime.iterdir() if p.name.lower() in ('memories', 'memory', 'claude.md', 'projects', 'plugins', 'agents')]
    if leftovers:
        raise ValueError('Client home must hold no memory or personal instructions: ' + ', '.join(leftovers))
    doc = worker_document(plan, ws, harness, cache_paths, starter_dt is not None)
    doc['native_system_skills'] = []
    write_json(ws / 'worker.json', doc)
    write_json(run / 'harness.json', harness)
    public = {'task/' + k: v for k, v in tree_manifest(ws / 'task').items()}
    public.update({'starter/' + k: v for k, v in tree_manifest(ws / 'starter').items()})
    if (ws / 'starter.dt').is_file():
        public['starter.dt'] = digest(ws / 'starter.dt')
    write_json(run / 'input-manifest.json', public)
    repo = Path(__file__).resolve().parents[1]
    probe = verify(ws, task / 'private' / 'fixtures.json', [task / 'private' / 'rules.json', repo / 'exam_bench' / 'grader.py', repo / '.local' / 'runs',
                                                             Path(auth_seed), Path.home() / '.claude', Path.home() / '.ssh'])
    write_json(run / 'isolation.json', probe)
    argv = [str(client), '-p', '--model', plan['model'], '--output-format', 'stream-json', '--verbose',
            '--setting-sources', 'user', '--dangerously-skip-permissions', '--no-session-persistence',
            '--disallowed-tools', *DENIED_TOOLS]
    if plan.get('reasoning_effort'):
        argv += ['--effort', plan['reasoning_effort']]
    argv += ['--strict-mcp-config']
    if mcp_config is not None:
        argv += ['--mcp-config', str(mcp_config)]
    env = worker_environment(ws, runtime, harness)
    if starter_dt is not None:
        # Prove that this worker can operate 1C before the token is added or the solve budget is spent.
        export = ws / 'platform-capability-export'
        export.mkdir()
        capability = supervise(wrapped([platform_binary(), 'DESIGNER', '/F', str(ws / 'ib'), '/DumpConfigToFiles', str(export),
                                        '/DisableStartupMessages', '/DisableStartupDialogs', '/Out', str(ws / 'platform-capability.log')], ws),
                               ws, env, '', 90, 3, run / 'worker-platform-capability')
        capability['configuration_exported'] = (export / 'Configuration.xml').is_file()
        write_json(run / 'worker-platform-capability.json', capability)
        if capability['status'] != 'completed' or not capability['stop_confirmed'] or not capability['configuration_exported']:
            raise ValueError('1C is unavailable inside the selected worker boundary; see worker-platform-capability.json')
        shutil.rmtree(export)
    write_json(run / 'plan.json', plan)
    write_json(run / 'plan-integrity.json', {'sha256': digest(run / 'plan.json')})
    write_json(run / 'client-profile.json', {'argv': argv, 'denied_tools': list(DENIED_TOOLS), 'settings': SETTINGS, 'web_search': 'disabled',
                                              'shell_network_access': True, 'external_network_profile': 'open-book', 'code_home': str(runtime),
                                              'native_version': plan['client_version'], 'models_fallback': False,
                                              'credential_readable_by_solver': True})
    # The token is added last and only to the process environment; it never enters an evidence file.
    token = Path(auth_seed).read_text(encoding='utf-8').strip()
    if not token:
        raise ValueError('Empty Claude Code OAuth token file')
    env['CLAUDE_CODE_OAUTH_TOKEN'] = token
    return ws, argv, env, public


def normalize(lines):
    """Telemetry of the stream-json output: per-model usage from the final result event, tool calls from messages."""
    from .telemetry import token_breakdown
    events, models, calls, results, limitations, malformed = [], set(), {}, {}, [], 0
    final = None
    for sequence, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        events.append({'source_sequence': sequence, 'native': event})
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init' and event.get('model'):
            models.add(event['model'])
        message = event.get('message') if isinstance(event.get('message'), dict) else {}
        if kind == 'assistant':
            if message.get('model'):
                models.add(message['model'])
            for block in message.get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    calls[block.get('id')] = block.get('name')
        if kind == 'user':
            for block in message.get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    results[block.get('tool_use_id')] = bool(block.get('is_error'))
        if kind == 'result':
            final = event
    tokens = {'input': None, 'cached_input': None, 'output': None, 'reasoning_output': None, 'total': None}
    cost = None
    if final and isinstance(final.get('modelUsage'), dict) and final['modelUsage']:
        usage = list(final['modelUsage'].values())
        fresh = sum(int(u.get('inputTokens') or 0) for u in usage)
        read = sum(int(u.get('cacheReadInputTokens') or 0) for u in usage)
        created = sum(int(u.get('cacheCreationInputTokens') or 0) for u in usage)
        tokens.update(input=fresh + read + created, cached_input=read, output=sum(int(u.get('outputTokens') or 0) for u in usage))
        tokens['total'] = tokens['input'] + tokens['output']
        tokens['cache_creation_input'] = created
        models |= set(final['modelUsage'])
        cost = final.get('total_cost_usd')
    else:
        limitations.append('No final result usage; incomplete attempts may have unreported spend')
    if malformed:
        limitations.append(f'{malformed} unparseable output lines')
    if not models:
        limitations.append('Native stream did not attest effective model')
    mcp = [{'server': name.split('__')[1], 'tool': name.split('__', 2)[2], 'status': 'failed' if results.get(i) else 'completed',
            'error': 'tool_result is_error' if results.get(i) else None} for i, name in calls.items() if name and name.startswith('mcp__') and name.count('__') >= 2]
    delegated = [n for n in calls.values() if n in ('Task', 'Agent')]
    return {'events': events, 'tokens': token_breakdown({**tokens, 'coverage': 'final_result_model_usage' if final else 'unavailable'}),
            'models_observed': sorted(models), 'model_provenance': 'client_reported' if models else 'requested_only',
            'agents': {'spawned_total': len(delegated), 'observed_children': len(delegated), 'coverage': 'native_delegation_denied' if not delegated else 'partial'},
            'limitations': list(dict.fromkeys(limitations)), 'native_cost_usd': cost,
            'tools': {'mcp_calls': len(mcp), 'unica_calls': sum(c['server'] == 'unica' for c in mcp), 'failed_calls': sum(c['status'] == 'failed' for c in mcp), 'calls': mcp}}


def start(plan, run, starter, task, client, auth_seed, prompt=None, starter_dt=None):
    import time
    prep = time.monotonic()
    ws, argv, env, public = prepare(plan, run, Path(starter), Path(task), client, auth_seed, starter_dt)
    run = Path(run).resolve()
    prep_ms = round((time.monotonic() - prep) * 1000)
    start_at = utc()
    prompt = solver_prompt(plan, prompt)
    write_json(run / 'solver-prompt.json', {'prompt': prompt, 'input_revision': plan['input_revision']})
    from .codex import portable_harness_run
    # Helpers (the 1C client, MCP daemons) never get the agent's credential.
    helper_env = {k: v for k, v in env.items() if k != 'CLAUDE_CODE_OAUTH_TOKEN'}
    portable = portable_harness_run(run)
    try:
        if portable:
            from .portable_harnesses import launch as launch_portable
            launch_portable(ws, helper_env, run)
        elif plan['harness'] == TOOLKIT:
            from .mcp_toolkit import launch
            launch(ws, helper_env, run)
        elif is_stdio_harness(plan['harness']):
            from .stdio_harnesses import launch as launch_stdio
            launch_stdio(plan['harness'], ws, helper_env, run)
        outcome = supervise(wrapped(argv, ws), ws, env, prompt, plan['solve_timeout_seconds'], plan['stop_grace_seconds'], run / 'raw')
    finally:
        env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
        if portable:
            from .portable_harnesses import stop as stop_portable
            write_json(run / 'runtime-cleanup.json', cleanup_portable := stop_portable(ws))
    if portable:
        outcome['stop_confirmed'] = outcome['stop_confirmed'] and cleanup_portable['stop_confirmed']
    elif is_stdio_harness(plan['harness']):
        from .stdio_harnesses import stop as stop_stdio
        cleanup = stop_stdio(plan['harness'], ws, env, plan['stop_grace_seconds'])
        write_json(run / 'runtime-cleanup.json', cleanup)
        outcome['stop_confirmed'] = outcome['stop_confirmed'] and cleanup['stop_confirmed']
    if plan['harness'] == TOOLKIT:
        from .mcp_toolkit import stop
        cleanup = stop(ws, plan['stop_grace_seconds'])
        write_json(run / 'runtime-cleanup.json', cleanup)
        outcome['stop_confirmed'] = outcome['stop_confirmed'] and cleanup['stop_confirmed']
    if is_unica(plan['harness']) and not portable:
        cleanup = stop_owned_runtimes(ws, plan['stop_grace_seconds'])
        write_json(run / 'runtime-cleanup.json', cleanup)
        outcome['stop_confirmed'] = outcome['stop_confirmed'] and cleanup['stop_confirmed']
    write_json(run / 'process.json', outcome)
    parsed = normalize((run / 'raw' / 'stdout.jsonl').read_text(errors='replace', encoding='utf-8').splitlines())
    metrics = {k: v for k, v in parsed.items() if k != 'events'}
    if settings()['isolation'] == 'dedicated-host':
        metrics['limitations'].append('Dedicated-host execution: no outer filesystem/network sandbox; not eligible for comparison')
    if portable:
        from .portable_harnesses import integrity
        unchanged = integrity(ws, run)
        write_json(run / 'harness-integrity.json', {'package_unchanged': unchanged})
        if not unchanged:
            metrics['limitations'].append('Solver changed the pinned native harness package')
    elif is_unica(plan['harness']):
        unchanged = tree_manifest(ws / 'harness/unica') == json.loads((run / 'unica-package-manifest.json').read_text(encoding='utf-8'))
        write_json(run / 'harness-integrity.json', {'package_unchanged': unchanged})
        if not unchanged:
            metrics['limitations'].append('Solver changed the pinned harness package')
    elif is_stdio_harness(plan['harness']):
        unchanged = tree_manifest(ws / 'harness' / plan['harness']) == json.loads((run / 'harness-package-manifest.json').read_text(encoding='utf-8'))
        write_json(run / 'harness-integrity.json', {'package_unchanged': unchanged})
        if not unchanged:
            metrics['limitations'].append('Solver changed the pinned harness package')
    elif plan['harness'] == TOOLKIT:
        unchanged = tree_manifest(ws / 'harness/mcp-toolkit') == json.loads((run / 'mcp-toolkit-package-manifest.json').read_text(encoding='utf-8'))
        write_json(run / 'harness-integrity.json', {'package_unchanged': unchanged})
        if not unchanged:
            metrics['limitations'].append('Solver changed the pinned harness package')
    elif plan['harness'] == 'shirokov-skills':
        unchanged = tree_manifest(ws / '.codex') == json.loads((run / 'shirokov-package-manifest.json').read_text(encoding='utf-8'))
        write_json(run / 'harness-integrity.json', {'package_unchanged': unchanged})
        if not unchanged:
            metrics['limitations'].append('Solver changed the pinned harness package')
    write_json(run / 'events.json', parsed['events'])
    error, adapter_compatible, ignored = None, True, []
    try:
        ignored = extra_sections(validate(ws, public, require_ready=True))
    except UnsupportedBindings as exc:
        error, adapter_compatible = str(exc), False
    except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        error = str(exc)
    manifest = None
    if outcome['stop_confirmed']:
        try:
            manifest = freeze(ws, run / 'artifacts')
        except ValueError as exc:
            error = str(exc)
    else:
        error = 'Stopping descendants was not confirmed; artifacts were not frozen'
    from .interpretations import read as read_interpretations
    interpretations = read_interpretations(run / 'artifacts') if manifest is not None else {'status': 'missing', 'items': []}
    cost = metrics.get('native_cost_usd')
    result = {'schema_version': 1, 'run_id': plan['run_id'], 'client': 'claude',
              'interpretations': {'status': interpretations['status'], 'count': len(interpretations['items'])},
              'started_at': start_at, 'process': outcome, 'time': {'prepare_ms': prep_ms, 'solve_ms': outcome['elapsed_ms'], 'grade_ms': None},
              'model': {'requested': plan['model'], 'observed': metrics['models_observed'], 'provenance': metrics['model_provenance']},
              'harness': plan['harness'], 'network_profile': 'open-book shell', 'metrics': metrics,
              'billing': {'channel': plan['billing_channel'], 'currency': 'USD' if cost is not None else None, 'actual_charge': None,
                          'api_equivalent': str(cost) if cost is not None else None, 'subscription_allocation': None,
                          'quota_before': None, 'quota_after': None, 'reason': 'Subscription; API equivalent is the client-reported total_cost_usd'},
              'submission': {'valid': error is None, 'adapter_compatible': adapter_compatible, 'error': error, 'ignored_binding_sections': ignored, 'files': len(manifest) if manifest is not None else None},
              'grade': {'scope': 'technical_partial', 'whole_ticket': 'not_evaluated', 'rules': [], 'status': 'not_run'}, 'comparison_eligible': False}
    machine = machine_identity()
    result['machine'] = machine
    result['isolation_verified'] = machine['isolation'] in ('macos', 'bubblewrap')
    write_json(run / 'result.json', result)
    return result
