"""Pre-exam readiness: find installed programs, report each step and what to do next.

Nothing here contacts a model. Paths come from discovery on this machine, never from HTTP;
the browser may only pick one of the discovered candidates.
"""
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STARTER = 'starters/carcass-8.3.25.1445'
VERSION = re.compile(r'8\.\d+\.\d+\.\d+')
PREFERRED_PLATFORM = '8.3.27.'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, data):
    from tools.demo_runner import atomic
    atomic(path, data)


def settings(root=ROOT):
    path = root / '.local/community/settings.json'
    return read(path) if path.is_file() else {}


def discover_platforms():
    """Installed 1cv8 executables, preferred version first."""
    if os.name == 'nt':
        bases = [Path(os.environ.get(k, '')) for k in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432') if os.environ.get(k)]
        found = [p for b in bases for p in b.glob('1cv8/*/bin/1cv8.exe')]
    else:
        found = [p for pattern in ('*/1cv8', '*/*/1cv8') for p in Path('/opt/1cv8').glob(pattern)]
        found += list(Path('/Applications').glob('1cv8*.app/Contents/MacOS/1cv8'))
    which = shutil.which('1cv8')
    if which:
        found.append(Path(which))
    result, seen = [], set()
    for path in found:
        path = path.resolve()
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        version = next((m.group(0) for part in reversed(path.parts) for m in [VERSION.fullmatch(part)] if m), None)
        result.append({'path': str(path), 'version': version})
    key = lambda c: (not (c['version'] or '').startswith(PREFERRED_PLATFORM), [-int(x) for x in (c['version'] or '0.0.0.0').split('.')])
    return sorted(result, key=key)


def discover_codex():
    from tools.installers import npm_codex
    found = [Path(p) for p in npm_codex()]
    which = shutil.which('codex')
    if which:
        found.append(Path(which))
    if os.name == 'nt' and os.environ.get('LOCALAPPDATA'):
        found += sorted(Path(os.environ['LOCALAPPDATA']).glob('OpenAI/Codex/bin/*/codex.exe'), reverse=True)
    found += [Path('/Applications/Codex.app/Contents/Resources/codex'), Path.home() / '.local/bin/codex']
    result = []
    for path in found:
        # An npm bin shim is a Node script; the native binary above runs without Node.
        if path.is_file() and path.suffix not in ('.js', '.cmd') and str(path.resolve()) not in result:
            result.append(str(path.resolve()))
    return result


# Agents and the models offered for each; a new model is added in this shipped file, not in code.
def agents():
    # Read on every call: an edited list shows up without restarting the server.
    return json.loads((ROOT / 'profiles/agents.json').read_text(encoding='utf-8'))['agents']


def model_allowed(agent, model):
    known = agents()
    return agent in known and any(m['id'] == model for m in known[agent]['models'])
CLAUDE_TOKEN = '.local/claude/oauth-token'


def codex_logged_in():
    home = Path(os.environ['CODEX_HOME']) if os.environ.get('CODEX_HOME') else Path.home() / '.codex'
    return (home / 'auth.json').is_file()


PROTECTION = re.compile(r'^\s*DisableUnsafeActionProtection\s*=\s*\.\*\s*$', re.M)
PROTECTION_LINE = 'DisableUnsafeActionProtection=.*'


def user_conf():
    """The 1C configuration file of the current OS account; writing it needs no administrator rights."""
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / '1C/1cv8/conf/conf.cfg'
    return Path.home() / '.1cv8/1C/1cv8/conf/conf.cfg'


def protection_disabled(binary=None):
    """Where unsafe-action protection is off for every base, or None while 1C would stop to ask."""
    files = [user_conf()]
    if binary:
        files += [Path(binary).parent / 'conf/conf.cfg', Path(binary).parent.parent / 'conf/conf.cfg', Path(binary).parents[2] / 'conf/conf.cfg']
    files.append(Path('/opt/1cv8/conf/conf.cfg'))
    for path in files:
        try:
            if path.is_file() and PROTECTION.search(path.read_text(encoding='utf-8-sig', errors='replace')):
                return path
        except OSError:
            continue
    return None


def isolation_mode():
    """The only boundary allowed on this OS, and whether it is usable now."""
    system = platform.system()
    if system == 'Darwin':
        return 'macos', Path('/usr/bin/sandbox-exec').is_file()
    if system == 'Linux':
        return 'bubblewrap', shutil.which('bwrap') is not None
    return 'dedicated-host', bool(settings().get('dedicated_host_confirmed'))


def step(id, title, state, detail, hint=None, actions=(), **extra):
    return {'id': id, 'title': title, 'state': state, 'detail': detail, 'hint': hint,
            'actions': [{'id': a[0], 'label': a[1], 'data': a[2] if len(a) > 2 else {}} for a in actions], **extra}


def selection(root=ROOT):
    """Discovered programs plus the user's pick among them."""
    chosen = settings(root)
    platforms, codex = discover_platforms(), discover_codex()
    platform_pick = next((c for c in platforms if c['path'] == chosen.get('platform')), platforms[0] if platforms else None)
    codex_pick = chosen.get('codex') if chosen.get('codex') in codex else (codex[0] if codex else None)
    from tools.installers import find
    mode, usable = isolation_mode()
    agent = chosen.get('agent') if chosen.get('agent') in agents() else 'codex'
    return {'platforms': platforms, 'platform': platform_pick, 'codex_candidates': codex, 'codex': codex_pick,
            'claude': find('claude'), 'agent': agent, 'isolation': mode, 'isolation_usable': usable}


def agent_client(root=ROOT):
    """Agent, executable and credential file for a new attempt; None when the agent step is not ready."""
    pick = selection(root)
    if pick['agent'] == 'claude':
        token = root / CLAUDE_TOKEN
        return ('claude', pick['claude'], token) if pick['claude'] and token.is_file() else None
    home = Path(os.environ['CODEX_HOME']) if os.environ.get('CODEX_HOME') else Path.home() / '.codex'
    return ('codex', pick['codex'], home / 'auth.json') if pick['codex'] and codex_logged_in() else None


def expected_machine(pick):
    return {'platform_binary': pick['platform']['path'], 'platform_version': pick['platform']['version'],
            'isolation': pick['isolation'], 'os': platform.system(), 'architecture': platform.machine()}


def calibration_state(root, pick):
    """ok / stale / missing for the calibration matching the selected platform and grader."""
    from exam_bench.preflight import verify_calibration
    path = root / '.local/demo/config.json'
    if not path.is_file():
        return 'missing', None
    try:
        cfg = read(path)
        calibration = read(root / cfg['calibration'])
        verify_calibration(calibration)
    except (OSError, KeyError, ValueError):
        return 'stale', 'Грейдер обновился — проверьте его заново.'
    if calibration.get('machine') != expected_machine(pick):
        return 'stale', 'Проверка делалась для другой платформы или режима изоляции — проверьте заново.'
    return 'ok', cfg


def harness_steps(root, action, calibrated):
    """Every shipped harness with its state on this machine and the next thing to do."""
    from tools.demo_runner import harness_options
    from tools.prepare_native_harnesses import catalogue, target
    options = {h['id']: h for h in harness_options(root) if h['id'] != 'none'}
    working = action.get('phase') == 'working' and action.get('action') == 'harness'
    items = []
    for name, doc in catalogue().items():
        item = {'id': name, 'label': doc['label'], 'description': doc.get('description'), 'homepage': doc.get('homepage'), 'action': None}
        receipt = root / '.local/harnesses' / name / 'capability.json'
        if working and action.get('id') == name:
            item.update(state='preparing', note='Скачиваем, устанавливаем и проверяем на каркасе…')
        elif target() not in doc['targets']:
            item.update(state='unavailable', note=f'Пакета для этой ОС ({target()}) пока нет.')
        elif options.get(name, {}).get('ready'):
            item.update(state='ready', note='Готов к экзамену.')
        elif name not in options and (root / '.local/harnesses' / name).is_dir() and any((root / '.local/harnesses' / name).iterdir()) \
                and not (action.get('action') == 'harness' and action.get('id') == name):
            from tools.prepare_native_harnesses import legacy_message
            item.update(state='failed', note=legacy_message(name))
        elif name in options:
            reason = read(receipt).get('reason') if receipt.is_file() else None
            item.update(state='failed', note='Проверка на каркасе не пройдена' + (': ' + reason if reason else '.'),
                        action={'id': 'prepare-harness', 'label': 'Повторить проверку'})
        else:
            failed = action.get('action') == 'harness' and action.get('id') == name and action.get('phase') == 'failed'
            item.update(state='failed' if failed else 'absent',
                        note=(action.get('error') or 'Подготовка не завершилась.') if failed else 'Не установлен.',
                        action={'id': 'prepare-harness', 'label': 'Подготовить'})
        if item['action'] and not calibrated:
            item['action'] = None
            item['note'] += ' Подготовка станет доступна после проверки грейдера.'
        items.append(item)
    return items


def install_action(tool):
    from tools.installers import install_command, PACKAGES
    return [('install', 'Установить ' + PACKAGES[tool]['label'], {'tool': tool})] if install_command(tool) else []


def failed_helper(current, kind):
    if current and current['kind'] == kind and current.get('returncode') not in (None, 0):
        return 'Не получилось: ' + (current['tail'][0] if current['tail'] else 'команда завершилась с ошибкой') + '. '
    return ''


def device_link(current, kind):
    if current and current['kind'] == kind and current['running'] and current['url']:
        return {'url': current['url'], 'code': current['code']}
    return None


def github_step(author, running, current):
    from tools.installers import find, manual_hint
    title = 'Аккаунт GitHub'
    if running in ('install-git', 'install-gh'):
        return step('github', title, 'working', 'Устанавливаем ' + ('Git' if running == 'install-git' else 'GitHub CLI') + '…',
                    'Окно установщика может попросить подтверждение.')
    if running == 'gh-login':
        link = device_link(current, 'gh-login')
        return step('github', title, 'working', 'Откройте ссылку, войдите в GitHub и введите код.' if link else 'Запускаем вход…',
                    'Страница обновится сама после входа.', link=link)
    if not find('git'):
        return step('github', title, 'missing', failed_helper(current, 'install-git') + 'Git не найден: он нужен для отправки результата.',
                    None if install_action('git') else manual_hint('git'), install_action('git') + [('refresh', 'Проверить снова', {})])
    if not find('gh'):
        return step('github', title, 'missing', failed_helper(current, 'install-gh') + 'GitHub CLI не найден: через него результат отправляется в рейтинг.',
                    None if install_action('gh') else manual_hint('gh'), install_action('gh') + [('refresh', 'Проверить снова', {})])
    if author.get('github'):
        return step('github', title, 'ok', f"{author['name']} · @{author['github']}",
                    'Под этим именем результат попадёт в рейтинг.', [('refresh', 'Обновить', {})])
    return step('github', title, 'missing', failed_helper(current, 'gh-login') + 'Вход в GitHub не выполнен.',
                'Откроется страница GitHub: войдите и введите показанный здесь код.',
                [('login', 'Войти в GitHub', {'kind': 'gh-login'}), ('refresh', 'Проверить снова', {})])


def agent_step(root, pick, running, current):
    from tools.installers import install_command, manual_hint, CLAUDE_INSTALL
    agent = pick['agent']
    known = agents()
    options = [{'value': k, 'label': v['label']} for k, v in known.items()]
    common = {'options': options, 'selected': agent, 'select_key': 'agent',
              'models': known[agent]['models'], 'default_model': known[agent]['default_model']}
    title = 'Агент'
    if running in ('install-node', 'install-codex'):
        return step('agent', title, 'working', 'Устанавливаем ' + ('Node.js' if running == 'install-node' else 'Codex CLI') + '…', None, **common)
    if agent == 'codex':
        if running == 'codex-login':
            link = device_link(current, 'codex-login')
            return step('agent', title, 'working', 'Откройте ссылку, войдите в ChatGPT и введите код.' if link else 'Запускаем вход…',
                        'Нужна подписка ChatGPT с доступом к Codex.', link=link, **common)
        if not pick['codex']:
            actions = install_action('codex') or install_action('node')
            hint = ('Сначала установите Node.js, затем Codex.' if not install_command('codex') and actions else
                    None if actions else manual_hint('codex'))
            return step('agent', title, 'missing', failed_helper(current, 'install-codex') + failed_helper(current, 'install-node') +
                        'Codex CLI не найден.', hint, actions + [('refresh', 'Проверить снова', {})], **common)
        if not codex_logged_in():
            return step('agent', title, 'missing', failed_helper(current, 'codex-login') + 'Codex найден, вход не выполнен.',
                        'Нужна подписка ChatGPT с доступом к Codex.',
                        [('login', 'Войти в Codex', {'kind': 'codex-login'}), ('refresh', 'Проверить снова', {})], **common)
        return step('agent', title, 'ok', 'Codex найден, вход выполнен.', pick['codex'], **common)
    if not pick['claude']:
        return step('agent', title, 'missing', 'Claude Code не найден.',
                    'Установите официальным установщиком: в терминале выполните ' + CLAUDE_INSTALL + ', затем «Проверить снова».',
                    [('refresh', 'Проверить снова', {})], copy=CLAUDE_INSTALL, **common)
    if not (root / CLAUDE_TOKEN).is_file():
        return step('agent', title, 'missing', 'Claude Code найден, нужен токен входа.',
                    'В терминале выполните claude setup-token, войдите в браузере и вставьте выданный токен сюда. '
                    'Нужна подписка Claude. Токен хранится только в .local/ на этом компьютере.',
                    copy='claude setup-token', input={'action': 'claude-token', 'placeholder': 'sk-ant-oat…', 'label': 'Сохранить токен'}, **common)
    return step('agent', title, 'ok', 'Claude Code найден, токен сохранён.', pick['claude'],
                [('forget-claude', 'Удалить токен', {})], **common)


def snapshot(root=ROOT):
    from exam_bench.common import digest
    pick = selection(root)
    action_file = root / '.local/community/action.json'
    action = read(action_file) if action_file.is_file() else {}
    working = action.get('phase') == 'working'
    if working:
        from tools.demo_runner import alive
        if not alive(action.get('pid')):
            working = False
            action.update(phase='failed', error='Рабочий процесс прерван. Повторите действие.')
    steps = []

    from tools import installers
    current = installers.helper(root)
    running = current['kind'] if current and current['running'] else None
    if current and current['kind'] == 'gh-login' and current.get('returncode') == 0 and not current.get('identity_read'):
        act('refresh', {}, root)  # the device login finished: read the account once
        state_path = root / '.local/community/helper.json'
        write(state_path, {**read(state_path), 'identity_read': True})
    author_file = root / '.local/community/operator.json'
    author = read(author_file) if author_file.is_file() else {}
    steps.append(github_step(author, running, current))

    if pick['platform'] and pick['platform']['version']:
        p = pick['platform']
        steps.append(step('platform', 'Платформа 1С', 'ok', f"1С {p['version']}", p['path'],
                          options=[{'value': c['path'], 'label': f"1С {c['version'] or '?'} · {c['path']}"} for c in pick['platforms']]
                          if len(pick['platforms']) > 1 else None, selected=p['path'], select_key='platform'))
    else:
        steps.append(step('platform', 'Платформа 1С', 'missing', 'Установленная платформа 1С не найдена.',
                          'Установите платформу 1С:Предприятие 8.3 (рекомендуется 8.3.27) и активируйте лицензию. '
                          'Дистрибутив — на releases.1c.ru; скачать его автоматически нельзя. '
                          'После установки нажмите «Проверить снова».', [('refresh', 'Проверить снова')], link={'url': 'https://releases.1c.ru', 'code': None}))

    off = protection_disabled(pick['platform']['path'] if pick['platform'] else None)
    if off:
        steps.append(step('protection', 'Защита от опасных действий 1С', 'ok', 'Отключена: 1С не будет останавливаться с вопросами.', str(off),
                          [('restore-protection', 'Вернуть защиту', {})] if off == user_conf() else []))
    else:
        steps.append(step('protection', 'Защита от опасных действий 1С', 'missing',
                          'Включена: 1С будет останавливаться и спрашивать разрешение, и экзамен не пройдёт без человека.',
                          f'Стенд допишет {PROTECTION_LINE} в {user_conf()} (с резервной копией). Это отключает защиту '
                          'для всех баз этой учётной записи — делайте так на тестовой машине. Вернуть можно кнопкой здесь же.',
                          [('disable-protection', 'Отключить защиту', {})]))

    steps.append(agent_step(root, pick, running, current))

    mode = pick['isolation']
    if mode == 'dedicated-host':
        steps.append(step('isolation', 'Изоляция агента', 'ok' if pick['isolation_usable'] else 'missing',
                          'Выделенная тестовая машина подтверждена.' if pick['isolation_usable'] else
                          'На Windows нет песочницы: агент получит доступ к этой учётной записи.',
                          'Запускайте только на отдельной машине или VM с демобазами. Такие результаты помечаются как неизолированные.',
                          [] if pick['isolation_usable'] else [('confirm-host', 'Это выделенная тестовая машина')]))
    else:
        label = {'macos': 'macOS sandbox-exec', 'bubblewrap': 'Linux bubblewrap'}[mode]
        steps.append(step('isolation', 'Изоляция агента', 'ok' if pick['isolation_usable'] else 'missing',
                          label if pick['isolation_usable'] else label + ' недоступен.',
                          None if pick['isolation_usable'] else 'В терминале: sudo apt install bubblewrap, затем «Проверить снова».',
                          [] if pick['isolation_usable'] else [('refresh', 'Проверить снова')]))

    starter = read(root / STARTER / 'starter.json')
    dt = root / STARTER / starter['file']
    starter_ok = dt.is_file() and digest(dt) == starter['sha256']
    steps.append(step('starter', 'Каркасная база', 'ok' if starter_ok else 'missing',
                      'Входит в стенд, контрольная сумма совпадает.' if starter_ok else 'Файл каркаса изменён или удалён.',
                      None if starter_ok else 'Восстановите файл: git checkout -- ' + STARTER))

    prior_ok = all(s['state'] == 'ok' for s in steps if s['id'] in ('platform', 'protection', 'isolation', 'starter'))
    if working and action.get('action') == 'setup':
        steps.append(step('calibration', 'Проверка грейдера', 'working', calibration_progress(root),
                          'Можно закрыть страницу: работа продолжится.'))
    elif not prior_ok:
        steps.append(step('calibration', 'Проверка грейдера', 'blocked', 'Станет доступна после шагов выше.'))
    else:
        state, info = calibration_state(root, pick)
        failed = action.get('action') == 'setup' and action.get('phase') == 'failed'
        if state == 'ok':
            steps.append(step('calibration', 'Проверка грейдера', 'ok',
                              'Грейдер работает на этой машине.'))
        elif failed:
            steps.append(step('calibration', 'Проверка грейдера', 'missing', action.get('error') or 'Прошлая проверка не прошла.',
                              'Проверьте, что 1С запускается вручную и лицензия активирована, затем повторите. '
                              'Журнал: .local/community/worker.log.', [] if working else [('calibrate', 'Повторить проверку')]))
        else:
            steps.append(step('calibration', 'Проверка грейдера', 'missing',
                              info if state == 'stale' else 'Нужна один раз для этой платформы и версии грейдера.',
                              'Короткая проверка, что грейдер работает на этой машине: 1С разворачивает каркас, '
                              'загружает правильное решение и решение с известной ошибкой и проверяет их на трёх сценариях. '
                              'Несколько минут, модель не вызывается.',
                              [] if working else [('calibrate', 'Проверить грейдер')]))

    calibrated = any(s['id'] == 'calibration' and s['state'] == 'ok' for s in steps)
    harnesses = harness_steps(root, action, calibrated)
    usable = [h for h in harnesses if h['state'] != 'unavailable']
    ready_count = sum(h['state'] == 'ready' for h in usable)
    steps.append(step('harnesses', 'Харнесы', 'working' if any(h['state'] == 'preparing' for h in usable) else
                      'ok' if usable and ready_count == len(usable) else 'optional',
                      f'Готово {ready_count} из {len(usable)} для этой ОС.' if usable else
                      'Для этой ОС пакетов харнесов пока нет. Доступен замер без харнеса.',
                      'Необязательно. Подготовленный харнес появится в выборе окружения для экзамена. '
                      'Свой харнес — файл harnesses/<id>.json, формат описан в docs/harnesses.md.',
                      items=harnesses, optional=True))

    required = [s for s in steps if not s.get('optional')]
    return {'ready': all(s['state'] == 'ok' for s in required), 'busy': working,
            'busy_action': action.get('action') if working else None, 'steps': steps}


def calibration_progress(root):
    setups = sorted((root / '.local/community/setup').glob('*'))
    latest = setups[-1] if setups else None
    if latest and (latest / 'smoke-check/progress.json').is_file():
        phase = read(latest / 'smoke-check/progress.json').get('phase')
        return {'positive': 'Проверяем правильное решение на трёх сценариях…', 'mutant': 'Проверяем, что известная ошибка ловится…'}.get(phase, 'Проверяем грейдер…')
    if latest and (latest / 'replay-calibration/progress.json').is_file():
        phase = read(latest / 'replay-calibration/progress.json').get('phase')
        return {'code': 'Сверяем код грейдера…', 'platform': 'Проверочный прогон в 1С…',
                'replay': 'Пересчитываем сохранённые наблюдения…'}.get(phase, 'Проверяем грейдер…')
    return 'Разворачиваем каркас в 1С…'


def act(action, data, root=ROOT):
    """Small synchronous actions; long ones go to community_service workers."""
    from tools.community_service import start
    if action == 'refresh':
        from tools.community_results import github_identity
        try:
            write(root / '.local/community/operator.json', github_identity())
        except ValueError:
            (root / '.local/community/operator.json').unlink(missing_ok=True)
        return {'saved': True}
    if action == 'install':
        from tools import installers
        if set(data) != {'tool'} or data['tool'] not in installers.PACKAGES:
            raise ValueError('Unknown tool')
        return installers.start('install-' + data['tool'], installers.install_command(data['tool']), root)
    if action == 'login':
        from tools import installers
        if data == {'kind': 'gh-login'}:
            return installers.start('gh-login', installers.login_command('gh-login'), root)
        if data == {'kind': 'codex-login'}:
            return installers.start('codex-login', installers.login_command('codex-login', selection(root)['codex']), root)
        raise ValueError('Unknown login')
    if action == 'claude-token':
        token = str(data.get('value', '')).strip() if set(data) == {'value'} else ''
        if not re.fullmatch(r'sk-ant-[A-Za-z0-9_\-]{20,}', token):
            raise ValueError('Это не похоже на токен из claude setup-token (он начинается с sk-ant-)')
        path = root / CLAUDE_TOKEN
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding='utf-8')
        path.chmod(0o600)
        return {'saved': True}
    if action == 'disable-protection':
        path = user_conf()
        text = path.read_text(encoding='utf-8-sig') if path.is_file() else ''
        if not PROTECTION.search(text):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                shutil.copyfile(path, path.with_name('conf.cfg.before-exam-bench.bak'))
            path.write_text(text + ('' if not text or text.endswith('\n') else '\n') + PROTECTION_LINE + '\n', encoding='utf-8')
        return {'saved': True}
    if action == 'restore-protection':
        path = user_conf()
        if path.is_file():
            path.write_text(PROTECTION.sub('', path.read_text(encoding='utf-8-sig')).replace('\n\n', '\n').lstrip('\n'), encoding='utf-8')
        return {'saved': True}
    if action == 'forget-claude':
        (root / CLAUDE_TOKEN).unlink(missing_ok=True)
        return {'saved': True}
    if action == 'select':
        if set(data) - {'platform', 'codex', 'agent'}:
            raise ValueError('Unknown selection')
        pick = selection(root)
        current = settings(root)
        if 'platform' in data:
            if data['platform'] not in {c['path'] for c in pick['platforms']}:
                raise ValueError('Платформа не найдена среди установленных')
            current['platform'] = data['platform']
        if 'codex' in data:
            if data['codex'] not in pick['codex_candidates']:
                raise ValueError('Codex не найден среди установленных')
            current['codex'] = data['codex']
        if 'agent' in data:
            if data['agent'] not in agents():
                raise ValueError('Неизвестный агент')
            current['agent'] = data['agent']
        write(root / '.local/community/settings.json', current)
        return {'saved': True}
    if action == 'confirm-host':
        if os.name != 'nt' or data != {'confirmed': True}:
            raise ValueError('Подтверждение нужно только на Windows')
        write(root / '.local/community/settings.json', {**settings(root), 'dedicated_host_confirmed': True})
        return {'saved': True}
    if action == 'calibrate':
        state = snapshot(root)
        blocking = [s['title'] for s in state['steps'] if s['id'] in ('platform', 'protection', 'isolation', 'starter') and s['state'] != 'ok']
        if blocking:
            raise ValueError('Сначала выполните шаги: ' + ', '.join(blocking))
        pick = selection(root)
        return start('setup', {'platform': pick['platform']['path'], 'version': pick['platform']['version'],
                               'isolation': pick['isolation']}, root)
    if action == 'prepare-harness':
        items = next(s for s in snapshot(root)['steps'] if s['id'] == 'harnesses')['items']
        item = next((h for h in items if h['id'] == data.get('id')), None)
        if set(data) != {'id'} or not item or not item['action']:
            raise ValueError('Этот харнес сейчас нельзя подготовить')
        return start('harness', {'id': data['id']}, root)
    raise ValueError('Unknown readiness action')
