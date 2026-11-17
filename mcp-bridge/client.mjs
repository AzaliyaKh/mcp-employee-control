import { setTimeout as sleep } from 'node:timers/promises';

/** Shared HTTP client for the two fixed MCP tools. No source-system credentials. */
export class WorklogClient {
  constructor({ baseUrl = 'http://127.0.0.1:8000', token }) {
    const url = new URL(baseUrl);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' ||
        url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
      throw new Error('Адрес приложения должен быть http://127.0.0.1:<порт>');
    }
    if (typeof token !== 'string' || token.length < 16) {
      throw new Error('Не настроен локальный токен подключения');
    }
    this.baseUrl = url.origin;
    this.token = token;
  }

  async request(path, data, signal) {
    const timeout = AbortSignal.timeout(10000);
    const response = await fetch(this.baseUrl + path, {
      method: data === undefined ? 'GET' : 'POST',
      headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      redirect: 'error',
    });
    if (!response.ok) throw new Error(`Локальное приложение вернуло HTTP ${response.status}`);
    return response.json();
  }

  resolveEmployee(query, { signal } = {}) {
    return this.request('/employees?query=' + encodeURIComponent(query), undefined, signal);
  }

  async statistics({ employeeId, start, end, kind = 'employee_statistics' },
                   options = {}) {
    let result = await this.request('/analysis', {
      kind, employee_id: employeeId, start, end,
    }, options.signal);
    return this.wait(result, options);
  }

  async privacyDecision({ run_id, ...decision }, options = {}) {
    const result = await this.request('/privacy/' + run_id, decision, options.signal);
    const receipt = result.decision_applied;
    return { ...await this.wait(result, options), decision_applied: receipt };
  }

  async wait(result, { signal, status = () => {}, pollMs = 500, timeoutMs = 1800000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (result.status === 'queued' || result.status === 'running') {
      if (!/^[0-9a-f]{32}$/.test(result.run_id)) throw new Error('Некорректный номер анализа');
      if (Date.now() >= deadline) {
        return { run_id: result.run_id, status: 'still_running',
                 message: 'Ожидание истекло; запрос продолжает выполняться локально.' };
      }
      status(`В процессе: ${result.stage}`);
      await sleep(pollMs, undefined, { signal });
      result = await this.request('/analysis/' + result.run_id, undefined, signal);
    }
    if (result.status === 'failed' || result.status === 'interrupted') {
      return { run_id: result.run_id, status: result.status, stage: result.stage,
               error: result.error || 'Анализ не завершён' };
    }
    if (result.status === 'awaiting_privacy_decision') return result;
    return { run_id: result.run_id, status: result.status,
             summary: result.summary, report_url: result.report_url };
  }
}
