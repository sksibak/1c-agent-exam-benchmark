import {$, node, get, post, ask, duration, scoreText, gradeText, checksText, harnessLabel} from './local.js';
import {renderSubmit} from './submit.js';
import {ADD_HARNESS_PROMPT} from './harness-prompt.js';

const ORDER = ['preparing', 'solving', 'checking_gates', 'grading', 'finished'];
const ACTIVE = new Set(['queued', 'preparing', 'solving', 'checking_gates', 'grading', 'publishing']);
const PHASES = {queued: 'Задание принято', preparing: 'Готовим чистую базу и агента', solving: 'Агент решает билет',
  checking_gates: 'Проверяем загрузку и компиляцию', grading: 'Проверяем сценарии в 1С', publishing: 'Считаем оценку',
  finished: 'Экзамен завершён', failed: 'Экзамен остановлен', invalid_submission: 'Корректная сдача не получена',
  rejected: 'Сдача не прошла обязательные условия'};
const MARKS = {ok: '✓', missing: '!', working: '…', blocked: '·', optional: '·'};
const store = {get: k => { try { return localStorage.getItem(k); } catch { return null; } },
               set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } }};
let busy = false, stepsSignature = '', launchError = null;

async function perform(fn) {
  busy = true;
  try { await fn(); launchError = null; } catch (error) { launchError = error.message; }
  finally { busy = false; await refresh(); }
}

// Actions that change the machine ask first; the rest run on click.
const confirmations = {
  install: a => `${a.label}? Стенд запустит установку через менеджер пакетов (winget, Homebrew или npm) и примет его условия.`,
  calibrate: () => 'Проверить грейдер? 1С несколько раз запустится в фоне, это займёт несколько минут. Страницу можно закрыть.',
};

async function runAction(a) {
  if (a.id === 'confirm-host') {
    if (await ask('Подтвердите: это отдельная тестовая машина или VM с демобазами. Агент получит доступ к этой учётной записи Windows.', 'Да, это тестовая машина'))
      await post('api/readiness/confirm-host', {confirmed: true});
    return;
  }
  const question = confirmations[a.id]?.(a);
  if (question && !await ask(question)) return;
  await post('api/readiness/' + a.id, a.data || {});
}

function copyLine(text) {
  const box = node('div', null, 'copy');
  const code = node('code', text);
  const b = node('button', 'Скопировать', 'btn small');
  b.addEventListener('click', () => navigator.clipboard?.writeText(text).then(() => { b.textContent = 'Скопировано'; }));
  box.append(code, b);
  return box;
}

const HARNESS_MARKS = {ready: '✓', preparing: '…', failed: '!', absent: '·', unavailable: '–'};

// A harness is added by the participant's own coding agent from a prompt; the stand then offers it.
function showHarnessPrompt() {
  const dialog = node('dialog', null, 'ask prompt-dialog');
  const text = node('textarea', null, 'review');
  text.value = ADD_HARNESS_PROMPT; text.readOnly = true; text.rows = 18;
  text.setAttribute('aria-label', 'Промпт для добавления харнеса');
  const copy = node('button', 'Скопировать промпт', 'btn primary'), close = node('button', 'Закрыть', 'btn');
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text.value); } catch { text.select(); document.execCommand('copy'); }
    copy.textContent = 'Скопировано';
  });
  close.addEventListener('click', () => { dialog.close(); dialog.remove(); });
  dialog.addEventListener('cancel', () => dialog.remove());
  const buttons = node('div', null, 'actions'); buttons.append(copy, close);
  dialog.append(node('h2', 'Добавить харнес'),
    node('p', 'Скопируйте промпт и отдайте его своему агенту (Codex или Claude Code), открытому в папке этого репозитория. '
      + 'В конце промпта допишите параметры харнеса. Агент создаст описание в harnesses/ и проверит установку; '
      + 'после этого харнес появится здесь с кнопкой «Подготовить».'), text, buttons);
  document.body.append(dialog);
  dialog.showModal();
}

function harnessList(items, standBusy) {
  const list = node('ul', null, 'harnesses');
  list.append(...items.map(h => {
    const li = node('li', null, 'harness ' + h.state);
    li.append(node('span', HARNESS_MARKS[h.state], 'mark'));
    const text = node('div');
    const title = h.homepage ? node('a', h.label) : node('b', h.label);
    if (h.homepage) { title.href = h.homepage; title.target = '_blank'; title.rel = 'noopener'; }
    text.append(title);
    if (h.description) text.append(node('div', h.description, 'hint'));
    text.append(node('div', h.note, 'state-text'));
    li.append(text);
    if (h.action) {
      const b = node('button', h.action.label, 'btn small' + (h.state === 'absent' ? ' primary' : ''));
      b.disabled = busy || standBusy;
      b.addEventListener('click', () => perform(() => post('api/readiness/' + h.action.id, {id: h.id})));
      li.append(b);
    }
    return li;
  }));
  return list;
}

function renderSteps(readiness) {
  const signature = JSON.stringify(readiness.steps) + busy + launchError;
  // Do not rebuild while the user is typing a token.
  if (document.activeElement?.closest?.('#steps form')) return;
  if (signature === stepsSignature) return;
  stepsSignature = signature;
  $('steps').replaceChildren(...readiness.steps.map(s => {
    const li = node('li', null, 'step ' + s.state);
    li.append(node('span', MARKS[s.state] || '·', 'mark'));
    const body = node('div');
    body.append(node('b', s.title), node('div', s.detail, 'state-text'));
    // With a choice list the selected path is already visible in it.
    if (s.hint && !(s.options && s.hint === s.selected)) body.append(node('div', s.hint, 'hint'));
    if (s.options) {
      const select = node('select', null, 'field');
      select.setAttribute('aria-label', s.title);
      select.append(...s.options.map(o => { const opt = node('option', o.label); opt.value = o.value; opt.disabled = !!o.disabled; return opt; }));
      select.value = s.selected;
      select.addEventListener('change', () => perform(() => post('api/readiness/select', {[s.select_key || s.id]: select.value})));
      body.append(select);
    }
    if (s.link) {
      const box = node('div', null, 'device');
      const open = node('a', s.link.code ? 'Открыть страницу входа' : 'Открыть ' + s.link.url.replace('https://', ''), 'btn small primary');
      open.href = s.link.url; open.target = '_blank'; open.rel = 'noopener';
      box.append(open);
      if (s.link.code) box.append(node('span', 'Код:'), copyLine(s.link.code));
      body.append(box);
    }
    if (s.copy) body.append(copyLine(s.copy));
    if (s.input) {
      const form = node('form', null, 'device');
      const field = node('input', null, 'field'); field.type = 'password'; field.placeholder = s.input.placeholder; field.autocomplete = 'off';
      field.setAttribute('aria-label', 'Токен');
      const save = node('button', s.input.label, 'btn small primary'); save.type = 'submit';
      form.append(field, save);
      form.addEventListener('submit', e => { e.preventDefault(); perform(() => post('api/readiness/' + s.input.action, {value: field.value})); });
      body.append(form);
    }
    if (s.items?.length) body.append(harnessList(s.items, readiness.busy));
    if (s.id === 'harnesses') {
      const add = node('button', 'Добавить харнес', 'btn small add-harness');
      add.addEventListener('click', showHarnessPrompt);
      body.append(add);
    }
    const acts = node('div', null, 'acts');
    acts.append(...s.actions.map(a => {
      const b = node('button', a.label, 'btn small' + (a.id === 'calibrate' ? ' primary' : ''));
      b.disabled = busy || (readiness.busy && a.id === 'calibrate');
      b.addEventListener('click', () => perform(() => runAction(a)));
      return b;
    }));
    li.append(body, acts);
    return li;
  }));
  const missing = readiness.steps.filter(s => !s.optional && s.state !== 'ok').map(s => '«' + s.title + '»');
  $('ready-summary').textContent = readiness.ready ? 'Всё готово к экзамену.' : 'Осталось: ' + missing.join(', ') + '.';
}

function renderLaunch(readiness, demo) {
  const harnesses = readiness.steps.find(s => s.id === 'harnesses')?.items || [];
  const agent = readiness.steps.find(s => s.id === 'agent');
  const options = [{id: 'none', label: 'Без харнеса', ready: true}, ...harnesses.filter(h => h.state !== 'unavailable')
    .map(h => ({id: h.id, label: h.label, ready: h.state === 'ready'}))];
  const signature = JSON.stringify(options);
  if ($('harness').dataset.signature !== signature) {
    const wanted = $('harness').dataset.signature ? $('harness').value : store.get('harness');
    $('harness').replaceChildren(...options.map(h => {
      const o = node('option', h.label + (h.ready ? '' : ' — сначала подготовьте')); o.value = h.id; o.disabled = !h.ready; return o;
    }));
    if (options.some(h => h.id === wanted && h.ready)) $('harness').value = wanted;
    $('harness').dataset.signature = signature;
  }
  // Models belong to the agent chosen above; the choice is remembered per agent.
  const models = agent?.models || [];
  const modelSignature = agent?.selected + JSON.stringify(models);
  if ($('model').dataset.signature !== modelSignature) {
    $('model').replaceChildren(...models.map(m => { const o = node('option', m.label + ' · ' + m.id); o.value = m.id; return o; }));
    const remembered = store.get('model-' + agent?.selected);
    $('model').value = models.some(m => m.id === remembered) ? remembered : agent?.default_model;
    $('model').dataset.signature = modelSignature;
    $('model').dataset.agent = agent?.selected || '';
  }
  if (!$('hours').dataset.loaded) { $('hours').value = store.get('hours') || '0.5'; $('hours').dataset.loaded = '1'; }
  const running = demo.job && ACTIVE.has(demo.job.phase);
  const model = agent ? `${agent.options.find(o => o.value === agent.selected)?.label}, ${$('model').value}` : null;
  $('launch-sub').textContent = 'Агент получает билет и каркас, решает его в отдельной рабочей папке, затем стенд проверяет сдачу.'
    + (model ? ` Агент: ${model}.` : '');
  $('start').disabled = busy || !readiness.ready || !demo.enabled || running || readiness.busy;
  $('harness').disabled = $('hours').disabled = $('model').disabled = running;
  $('regrade-line').hidden = !demo.regrade_available;
  $('regrade').disabled = busy || running || readiness.busy || !demo.enabled;
  $('start-why').textContent = running ? 'Экзамен уже идёт — следите за ходом ниже.'
    : readiness.busy ? 'Дождитесь окончания подготовки стенда.'
    : !readiness.ready ? 'Кнопка станет активной, когда все обязательные шаги выше будут отмечены ✓.'
    : 'Используется ваша подписка. Закрытие страницы не прерывает экзамен.';
  $('start-message').hidden = !launchError;
  $('start-message').textContent = launchError || '';
  $('ready-message').hidden = !launchError;
  $('ready-message').textContent = launchError || '';
}

function renderJob(demo, results, githubReady) {
  const job = demo.job;
  $('job-panel').hidden = !job;
  if (!job) return;
  const running = ACTIVE.has(job.phase);
  $('job-title').textContent = running ? '3. Ход экзамена' : '3. Итог экзамена';
  const at = job.phase === 'publishing' ? 4 : job.phase === 'queued' ? 0 : ORDER.indexOf(job.phase);
  for (const li of $('timeline').children) {
    const i = ORDER.indexOf(li.dataset.phase);
    li.className = job.phase === 'finished' || (at >= 0 && i < at) ? 'done' : i === at && running ? 'current' : '';
  }
  const kv = [['Агент', [job.agent === 'claude' ? 'Claude Code' : 'Codex', job.model].filter(Boolean).join(' · ')], ['Окружение', harnessLabel(job.harness)], ['Состояние', PHASES[job.phase] || job.phase],
    ['Прошло', duration(job.elapsed_seconds || 0) + (job.timeout_seconds ? ` · лимит решения ${duration(job.timeout_seconds)}` : '')]];
  if (job.phase === 'grading') kv.push(['Сценарии', `${job.cases_completed || 0} из 23`]);
  $('job-kv').replaceChildren(...kv.map(([k, v]) => { const s = node('span', k + ': '); s.append(node('b', v)); return s; }));
  $('job-activity').textContent = job.phase === 'solving' ? `Агент работает: журнал ${Math.round((job.activity_bytes || 0) / 1024)} КБ.` : '';
  $('job-error').hidden = !job.error;
  $('job-error').textContent = job.error || '';
  const item = results.find(r => r.job_id === job.job_id);
  const e = item?.evaluation;
  $('outcome').hidden = !(job.phase === 'finished' && e);
  if (!e || job.phase !== 'finished') return;
  $('outcome-score').textContent = 'Балл ' + scoreText(e);
  const kv2 = [['Проверки', checksText(e)], ['Оценка', gradeText(e) || '—'], ['Без оценки', String(e.not_assessed)], ['Время решения', duration(e.solve_seconds)]];
  $('outcome-kv').replaceChildren(...kv2.map(([k, v]) => { const s = node('span', k + ': '); s.append(node('b', v)); return s; }));
  $('outcome-charges').hidden = !e.charges.length;
  $('charges').replaceChildren(...e.charges.map(c => node('li', `${c.id} · −${String(c.points).replace('.', ',')} · ${c.reason || ''}`)));
  if (job.mode === 'run') renderSubmit($('submit'), item, demo.community, githubReady, refresh);
}

async function refresh() {
  let readiness;
  try { readiness = await get('api/readiness'); }
  catch {
    $('offline').hidden = false;
    for (const id of ['ready-panel', 'launch-panel', 'job-panel']) $(id).hidden = true;
    return;
  }
  $('offline').hidden = true;
  const [demo, results, community] = await Promise.all([
    get('api/demo').catch(() => ({enabled: false})), get('api/results').then(r => r.results).catch(() => []),
    get('api/community').catch(() => null)]);
  demo.community = community;
  renderSteps(readiness);
  renderLaunch(readiness, demo);
  renderJob(demo, results, readiness.steps.find(s => s.id === 'github')?.state === 'ok');
}

$('harness').addEventListener('change', () => store.set('harness', $('harness').value));
$('hours').addEventListener('change', () => store.set('hours', $('hours').value));
$('model').addEventListener('change', () => store.set('model-' + $('model').dataset.agent, $('model').value));
$('start').addEventListener('click', () => perform(async () => {
  const limit = $('hours').selectedOptions[0].textContent;
  if (!await ask(`Начать экзамен? Агент будет решать билет до ${limit}, затем стенд проверит решение в 1С. Используется ваша подписка.`, 'Начать экзамен')) return;
  await post('api/demo/start', {mode: 'run', harness: $('harness').value, hours: Number($('hours').value), model: $('model').value});
}));
$('regrade').addEventListener('click', () => perform(() => post('api/demo/start', {mode: 'regrade'})));
refresh();
setInterval(() => { if (!busy) refresh(); }, 4000);
