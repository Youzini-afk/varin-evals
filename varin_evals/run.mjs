// Non-interactive controller of Varin's public Host APIs. Varin executes the task.
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { privateValueRedactor, resolveConfiguration } from './configuration.mjs';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const terminal = new Set(['completed', 'failed', 'cancelled']);

export async function runTrial({ runtime, config, instruction, workdir, output, keepHost = false, startupTimeout = 120 }) {
  config = resolveConfiguration(config);
  const redact = privateValueRedactor(config);
  runtime = path.resolve(runtime);
  workdir = path.resolve(workdir);
  output = path.resolve(output);
  await mkdir(output, { recursive: false }); // Never overwrite an earlier attempt.
  const privateDir = await mkdtemp(path.join(os.tmpdir(), 'varin-eval-'));
  const rawDir = path.join(privateDir, 'records');
  await mkdir(rawDir, { mode: 0o700 });
  const dataDir = path.join(privateDir, 'data');
  await mkdir(dataDir, { mode: 0o700 });
  await writeFile(path.join(dataDir, 'models.json'), JSON.stringify(config.models), { mode: 0o600 });
  await writeFile(path.join(dataDir, 'agent-settings.json'), JSON.stringify(config.agentSettings ?? {}));
  if (config.mcp) await writeFile(path.join(dataDir, 'mcp.json'), JSON.stringify(config.mcp));
  await writeFile(path.join(dataDir, 'settings.json'), JSON.stringify({ projects: [{ id: 'task', label: 'Benchmark task', path: workdir }] }));
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const password = randomUUID();
  const log = createWriteStream(path.join(privateDir, 'host.log'));
  await new Promise((resolve, reject) => { log.once('open', resolve); log.once('error', reject); });
  const node = path.join(runtime, 'bin/node');
  const cli = path.join(runtime, 'app/packages/web/bin/cli.js');
  const host = spawn(node, [cli, 'serve', '--foreground', '--api-only', '--host', '127.0.0.1', '--port', String(port), '--quiet', '--no-qr'], {
    cwd: workdir, detached: true, stdio: ['ignore', log.fd, log.fd],
    env: { ...process.env, PATH: `${path.join(runtime, 'bin')}${path.delimiter}${process.env.PATH}`, NODE_ENV: 'production',
      VARIN_DATA_DIR: dataDir, VARIN_WORKSPACE_ROOT: workdir, VARIN_UI_PASSWORD: password },
  });
  let hostError;
  host.on('error', error => { hostError = error; });
  const hostExited = new Promise(resolve => host.once('exit', (code, signal) => resolve({ code, signal })));
  let cookie;
  const request = async (endpoint, body, { timeout = 60_000 } = {}) => {
    const response = await fetch(`${base}${endpoint}`, { method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', ...(cookie ? { Cookie: cookie } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(timeout) });
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}));
      throw new Error(`${endpoint}: HTTP ${response.status} ${failure.code ?? failure.error ?? ''}`);
    }
    return response;
  };
  const api = async (method, body) => (await request(`/api/threads/${method}`, body)).json();
  const save = async (name, value) => {
    await writeFile(path.join(rawDir, name), JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
    await writeFile(path.join(output, name), JSON.stringify(redact(value), null, 2) + '\n');
  };
  const start = Date.now();
  const record = { schemaVersion: 1, startedAt: new Date(start).toISOString(),
    configurationSha256: createHash('sha256').update(JSON.stringify(config)).digest('hex'),
    instructionSha256: createHash('sha256').update(instruction).digest('hex'),
    model: config.model, workFocus: config.workFocus ?? 'code', outcome: 'preparing', exportErrors: [] };
  let receipt, identity, interrupted = false, exportsComplete = false;
  const interrupt = () => { interrupted = true; };
  process.on('SIGTERM', interrupt);
  process.on('SIGINT', interrupt);
  try {
    record.runtime = JSON.parse(await readFile(path.join(runtime, 'bundle.json'), 'utf8'));
    await save('run.json', record);
    while (true) {
      if (hostError) throw hostError;
      if (host.exitCode !== null || host.signalCode !== null) throw new Error(`Varin exited during startup; private diagnostics: ${privateDir}`);
      if (interrupted) throw new Error('Interrupted during startup');
      try { await request('/health', undefined, { timeout: 1000 }); break; }
      catch (error) { if (Date.now() - start > startupTimeout * 1000) throw new Error(`Host startup failed; private diagnostics: ${privateDir}`, { cause: error }); }
      await sleep(250);
    }
    const login = await request('/auth/session', { password });
    cookie = login.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
    let profileId = null;
    if (config.nativeTools !== undefined) {
      const profiles = await (await request('/api/agent-profiles/read', { runtime: 'agent' })).json();
      const saved = await (await request('/api/agent-profiles/save', { runtime: 'agent', expectedRevision: profiles.revision,
        config: { kind: 'custom', key: '', agent: { name: 'Benchmark', description: 'Benchmark tool configuration',
          instructions: '', enabled: true, workFocus: [config.workFocus ?? 'general'] } }, tools: config.nativeTools })).json();
      profileId = saved.profileId;
    }
    const key = randomUUID();
    identity = await api('create', { key });
    record.identity = identity;
    await save('run.json', record);
    const resolved = await api('source/resolve', { ...identity, resourceOwner: { kind: 'local' }, path: workdir });
    const prepared = await api('source/prepare', { ...identity, key: `${key}:source`, resourceOwner: resolved.resourceOwner,
      workspaceId: resolved.workspaceId, mode: 'live_root', content: { mode: 'saved-files' }, captureScopes: [] });
    receipt = await api('submit', { ...identity, key: `${key}:input`, expectedHead: null, text: instruction,
      source: prepared.source, model: config.model, agent: { profileId, workFocus: config.workFocus ?? 'code' } });
    record.receipt = receipt;
    record.outcome = 'accepted';
    await save('run.json', record);
    const states = createWriteStream(path.join(output, 'states.jsonl'));
    let previous;
    try {
      while (true) {
        if (interrupted) await api('run/cancel', { runId: receipt.run_id });
        const run = await api('run', { runId: receipt.run_id });
        record.run = run;
        if (run.state !== previous) {
          states.write(JSON.stringify({ at: new Date().toISOString(), state: run.state }) + '\n');
          previous = run.state;
        }
        if (terminal.has(run.state)) { record.outcome = run.state; break; }
        if (interrupted) { record.outcome = 'cancellation_requested'; break; }
        await sleep(1000);
      }
    } finally { await new Promise(resolve => states.end(resolve)); }
  } catch (error) {
    record.outcome = interrupted ? 'interrupted' : 'error';
    record.error = error.message;
  } finally {
    // A failed export remains a failed export; it never becomes an empty successful trace.
    const collect = async (name, read) => {
      try { const result = await read(); await save(name, result); return result; }
      catch (error) { record.exportErrors.push({ file: name, error: error.message }); }
    };
    if (identity) {
      const snapshot = await collect('snapshot.json', () => api('snapshot', identity));
      if (snapshot?.runFailure) record.failure = snapshot.runFailure;
      if (snapshot) {
        await collect('conversation.json', async () => (await request('/api/threads/export', { ...identity,
          headId: snapshot.historyPage.head, format: 'json', includeTools: true, includeAttachments: false })).json());
      }
      const family = await collect('family.json', async () => {
        const members = []; let cursor;
        do {
          const page = await api('family/list', { ...identity, includeSelf: true, ...(cursor ? { cursor } : {}) });
          members.push(...page.members); cursor = page.nextCursor;
        } while (cursor);
        return { members };
      });
      if (family) await collect('histories.json', async () => {
        const threads = [];
        const targets = [{ identity, name: 'Varin', parentThreadId: null }];
        for (const member of family.members) for (const branch of member.branches) targets.push({
          identity: { runtime: 'agent', threadId: member.threadId, branchId: branch.branchId },
          name: member.name, parentThreadId: member.parentThreadId,
        });
        const seen = new Set();
        for (const participant of targets) {
          const target = participant.identity;
          const key = JSON.stringify(target);
          if (seen.has(key)) continue;
          seen.add(key);
          const view = key === JSON.stringify(identity) && snapshot ? snapshot : await api('snapshot', target);
          const items = [...view.history]; let previous = view.historyPage.previous;
          while (previous) {
            const page = await api('history/page', { ...target, headId: view.historyPage.head, beforeId: previous });
            items.unshift(...page.items);
            if (page.previous === previous) throw new Error('History cursor did not advance');
            previous = page.previous;
          }
          threads.push({ ...participant, items });
        }
        return { root: identity, threads };
      });
      await collect('branch-usage.json', () => api('usage', identity));
      await collect('usage.json', async () => (await request('/api/usage/summary', {
        fromMs: start, toMs: Date.now() + 1, utcOffsetMinutes: 0,
      })).json()); // Fresh Catalog: all models, child tasks, compaction and auxiliary calls.
      exportsComplete = record.exportErrors.length === 0;
    }
    record.hostRetainedForVerifier = keepHost && record.outcome === 'completed';
    if (record.hostRetainedForVerifier) {
      host.unref();
    } else {
      try { if (receipt && !terminal.has(record.outcome)) await api('run/cancel', { runId: receipt.run_id }); }
      catch (error) { record.cancellationError = error.message; }
      try { await request('/api/system/shutdown', {}); } catch { /* Process stop below is authoritative. */ }
      let stopped = await Promise.race([hostExited, sleep(10_000).then(() => null)]);
      if (!stopped) {
        try { process.kill(-host.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
        stopped = await Promise.race([hostExited, sleep(10_000).then(() => null)]);
      }
      if (!stopped) {
        try { process.kill(-host.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
        stopped = await hostExited;
      }
      record.hostExit = stopped;
    }
    log.end();
    process.off('SIGTERM', interrupt); process.off('SIGINT', interrupt);
    record.finishedAt = new Date().toISOString();
    record.elapsedSeconds = (Date.now() - start) / 1000;
    record.exportsComplete = exportsComplete;
    await save('run.json', record);
  }
  if (record.outcome !== 'completed' || !exportsComplete) throw new Error(`Trial ended ${record.outcome}; see ${path.join(output, 'run.json')}`);
  return record;
}

if (process.argv[1] && import.meta.url === (await import('node:url')).pathToFileURL(process.argv[1]).href) {
  const { values } = parseArgs({ options: { runtime: { type: 'string' }, config: { type: 'string' }, task: { type: 'string' },
    workdir: { type: 'string' }, output: { type: 'string' }, 'keep-host': { type: 'boolean', default: false } } });
  try {
    for (const key of ['runtime', 'config', 'task', 'workdir', 'output']) if (!values[key]) throw new Error(`Missing --${key}`);
    await runTrial({ runtime: values.runtime, config: JSON.parse(await readFile(values.config, 'utf8')),
      instruction: await readFile(values.task, 'utf8'), workdir: values.workdir, output: values.output, keepHost: values['keep-host'] });
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
