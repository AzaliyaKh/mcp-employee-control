import { readFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { WorklogClient } from './client.mjs';

const VERSIONS = ['2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05'];
const MAX_MESSAGE = 64 * 1024;
const tools = [
  { name: 'resolve_employee', description: 'Найти сотрудника по имени. Если кандидатов несколько, уточни выбор.',
    inputSchema: { type: 'object', properties: { query: { type: 'string', minLength: 1, maxLength: 256 } },
      required: ['query'], additionalProperties: false },
    annotations: { readOnlyHint: true, openWorldHint: false } },
  { name: 'get_employee_statistics', description: 'ЛОКАЛЬНАЯ статистика сотрудника без облака, маскировки и вопросов об отправке. Сначала resolve_employee. Даты YYYY-MM-DD. Выведи отдельно «Выполненные задачи» по summary.completed_tasks и «Текущие задачи» по summary.current_tasks: перечисли названия title, при наличии url сделай ссылкой. Просроченные покажи ТОЛЬКО числом summary.counts.overdue, без отдельного списка. Не выдумывай названия и не меняй числа. Для статистики используй ТОЛЬКО эту функцию; покажи [Открыть отчёт](report_url).',
    inputSchema: { type: 'object', properties: {
      employee_id: { type: 'string', pattern: '^EMP_[0-9]+$' },
      start: { type: 'string', pattern: '^\\d{4}-\\d{2}-\\d{2}$' },
      end: { type: 'string', pattern: '^\\d{4}-\\d{2}-\\d{2}$' } },
      required: ['employee_id', 'start', 'end'], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false } },
];

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const idValid = id => typeof id === 'string' || (typeof id === 'number' && Number.isSafeInteger(id));
const exactKeys = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k));

function validateArguments(name, args) {
  if (name === 'privacy_decision') {
    return object(args) && ['run_id', 'task_id', 'version', 'action'].every(k => Object.hasOwn(args, k)) &&
      Object.keys(args).every(k => ['run_id', 'task_id', 'version', 'action', 'cleaned_text'].includes(k)) &&
      /^[0-9a-f]{32}$/.test(args.run_id) && Number.isSafeInteger(args.task_id) && args.task_id > 0 &&
      /^[0-9a-f]{64}$/.test(args.version) && ['continue', 'skip', 'clean'].includes(args.action) &&
      (args.action !== 'clean' || (typeof args.cleaned_text === 'string' && args.cleaned_text.length <= 1000));
  }
  if (name === 'resolve_employee') {
    return exactKeys(args, ['query']) && typeof args.query === 'string' && args.query.trim().length > 0 && args.query.length <= 256;
  }
  return exactKeys(args, ['employee_id', 'start', 'end']) &&
    typeof args.employee_id === 'string' && /^EMP_[0-9]+$/.test(args.employee_id) &&
    ['start', 'end'].every(k => typeof args[k] === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(args[k]));
}

/** Minimal MCP stdio transport for this fixed tool set; no SDK or npm dependencies. */
function start(client, enablePrivacy = false) {
  if (enablePrivacy) tools.push({ ...tools[1], name: 'analyze_employee_work',
    description: 'Облачный анализ содержания работ и текстовых отчётов Bitrix через Codex. Вызывай только если пользователь явно просит качественный анализ/пояснение облачного ИИ или Codex; для подсчёта задач используй get_employee_statistics. Текст обезличивается перед отправкой. При оставшихся данных покажи название конкретной задачи и вопрос из privacy.question. Всегда выведи ссылку report_url.' });
  if (enablePrivacy) tools.push({ name: 'privacy_decision',
    description: 'Решение только по показанной по НАЗВАНИЮ задаче перед облачной отправкой. Ответ «нет», «не отправлять» или «пропустить» означает action=skip: исключить ТОЛЬКО эту задачу из облачного анализа и отметить в отчёте. «Продолжить»/явное согласие означает action=continue: отправить показанную версию как есть. Не проси пользователя вводить ID: возьми task_id и version из текущего вопроса. Выполни ровно один вызов. Следующая задача требует НОВОГО ответа. clean заменяет первое значение на cleaned_text и повторяет проверку.',
    inputSchema: { type: 'object', additionalProperties: false,
      properties: { run_id: { type: 'string' }, task_id: { type: 'integer' }, version: { type: 'string' },
        action: { type: 'string', enum: ['continue', 'skip', 'clean'] }, cleaned_text: { type: 'string', maxLength: 1000 } },
      required: ['run_id', 'task_id', 'version', 'action'] },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false } });
  let initialized = false;
  let ready = false;
  let buffer = Buffer.alloc(0);
  const pending = new Map();
  const send = message => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
  const error = (id, code, message) => send({ id, error: { code, message } });
  const result = (id, value) => send({ id, result: value });

  async function handle(message) {
    if (!object(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string' ||
        (Object.hasOwn(message, 'id') && !idValid(message.id))) {
      error(null, -32600, 'Invalid request');
      return;
    }
    const hasId = Object.hasOwn(message, 'id');
    const params = message.params ?? {};
    if (!hasId) {
      if (message.method === 'notifications/initialized' && initialized) ready = true;
      if (message.method === 'notifications/cancelled' && object(params)) {
        const request = pending.get(params.requestId);
        if (request) { request.cancelled = true; request.controller.abort(); }
      }
      return;
    }
    const id = message.id;
    if (!object(params)) { error(id, -32602, 'Invalid params'); return; }
    if (pending.has(id)) { error(id, -32600, 'Duplicate active request id'); return; }
    if (message.method === 'initialize') {
      if (initialized || typeof params.protocolVersion !== 'string' || !object(params.capabilities) || !object(params.clientInfo)) {
        error(id, -32602, 'Invalid initialization'); return;
      }
      initialized = true;
      result(id, { protocolVersion: VERSIONS.includes(params.protocolVersion) ? params.protocolVersion : VERSIONS[0],
        capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'employee-worklog', version: '1.0.0' },
        instructions: 'Use get_employee_statistics for counts without cloud or privacy questions. Use analyze_employee_work only for an explicit request for qualitative cloud/Codex analysis. Privacy decisions apply to one task shown by title; no means skip, continue means send the shown version. Never invent counts. Source systems are accessed by the service.' });
      return;
    }
    if (message.method === 'ping') { result(id, {}); return; }
    if (!ready) { error(id, -32000, 'Initialize the connection first'); return; }
    if (message.method === 'tools/list') {
      if (params.cursor !== undefined) { error(id, -32602, 'No additional pages'); return; }
      result(id, { tools }); return;
    }
    if (message.method !== 'tools/call') { error(id, -32601, 'Method not found'); return; }
    if (!tools.some(tool => tool.name === params.name)) { error(id, -32602, 'Unknown tool'); return; }
    if (!validateArguments(params.name, params.arguments)) { error(id, -32602, 'Invalid tool arguments'); return; }
    const progressToken = params._meta?.progressToken;
    if (progressToken !== undefined && !idValid(progressToken)) { error(id, -32602, 'Invalid progress token'); return; }
    if (pending.size >= 4) { error(id, -32000, 'Too many active requests'); return; }
    const request = { controller: new AbortController(), cancelled: false };
    pending.set(id, request);
    let progress = 0;
    const status = message => {
      if (progressToken !== undefined && !request.cancelled) {
        send({ method: 'notifications/progress', params: { progressToken, progress: ++progress, message } });
      }
    };
    try {
      status('В процессе: запрос к сервису');
      const data = params.name === 'privacy_decision'
        ? await client.privacyDecision(params.arguments, { signal: request.controller.signal, status })
        : params.name === 'resolve_employee'
        ? await client.resolveEmployee(params.arguments.query, { signal: request.controller.signal })
        : await client.statistics({ employeeId: params.arguments.employee_id, start: params.arguments.start,
            end: params.arguments.end,
            kind: params.name === 'analyze_employee_work' ? 'employee_review' : 'employee_statistics' },
            { signal: request.controller.signal, status });
      if (!request.cancelled) {
        const content = [{ type: 'text', text: JSON.stringify(data) }];
        if (data.report_url) {
          content.push({ type: 'text', text: 'Добавь в ответ пользователю эту ссылку целиком:\n' +
            `[Открыть отчёт](${data.report_url})\nБраузер не открывался. Пользователь сам открывает ссылку.` });
        }
        if (data.status === 'awaiting_privacy_decision') {
          content.push({ type: 'text', text: 'Покажи НАЗВАНИЕ текущей задачи (task_title), найденные данные и вопрос privacy[0].question. Пользователь отвечает словами, номер задачи вводить не нужно. «Нет» означает skip только этой задачи; «Продолжить» — continue только этой версии. Для следующей задачи спроси отдельно и дождись НОВОГО ответа. Остальные проверенные задачи продолжают анализироваться.' });
        }
        if (data.decision_applied) {
          content.push({ type: 'text', text: `Подтверждено сервисом: действие ${data.decision_applied.action} применено только к задаче ${data.decision_applied.task_id}. Не утверждай, что пропущены все задачи. Если статус awaiting_privacy_decision, покажи следующий вопрос и заверши ответ, не вызывая инструменты повторно.` });
        }
        result(id, { content, isError: false });
      }
    } catch {
      if (!request.cancelled) result(id, { content: [{ type: 'text',
        text: 'Не удалось выполнить функцию. Проверьте локальный сервис, токен и параметры запроса. Не придумывайте результат.' }], isError: true });
    } finally {
      pending.delete(id);
    }
  }

  process.stdin.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    let boundary;
    while ((boundary = buffer.indexOf(10)) !== -1) {
      const line = buffer.subarray(0, boundary);
      buffer = buffer.subarray(boundary + 1);
      if (line.length > MAX_MESSAGE) { error(null, -32600, 'Message too large'); process.exitCode = 1; process.stdin.destroy(); return; }
      try {
        const text = new TextDecoder('utf-8', { fatal: true }).decode(line);
        if (text.trim()) void handle(JSON.parse(text)).catch(() => error(null, -32603, 'Internal error'));
      } catch { error(null, -32700, 'Parse error'); }
    }
    if (buffer.length > MAX_MESSAGE) { error(null, -32600, 'Message too large'); process.exitCode = 1; process.stdin.destroy(); }
  });
  process.stdin.on('end', () => {
    for (const request of pending.values()) { request.cancelled = true; request.controller.abort(); }
  });
  process.stdin.on('close', () => {
    for (const request of pending.values()) { request.cancelled = true; request.controller.abort(); }
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
    for (const request of pending.values()) request.controller.abort();
    process.exit(0);
  });
}

try {
  const { values } = parseArgs({ options: { address: { type: 'string', default: 'http://127.0.0.1:8000' },
                'token-file': { type: 'string' }, 'enable-privacy': { type: 'boolean', default: false } }, strict: true });
  const token = values['token-file'] ? readFileSync(values['token-file'], 'utf8').trim() : process.env.WORKLOG_TOKEN;
  start(new WorklogClient({ baseUrl: values.address, token }), values['enable-privacy']);
} catch {
  console.error('MCP bridge: не удалось прочитать токен или настройки. Используйте подготовленную конфигурацию.');
  process.exitCode = 1;
}
