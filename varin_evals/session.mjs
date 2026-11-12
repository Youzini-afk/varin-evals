// Multi-turn benchmark transport. Native Varin owns every model/tool exchange.
import { randomUUID } from 'node:crypto';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import { parseArgs } from 'node:util';
import { runTrial } from './run.mjs';
import { delay, exportNativeRecords, recordWriter, terminalRun } from './control.mjs';

const { values } = parseArgs({ options: {
  runtime: { type: 'string' }, config: { type: 'string' }, workdir: { type: 'string' }, output: { type: 'string' },
} });
for (const key of ['runtime', 'config', 'workdir', 'output']) if (!values[key]) throw new Error(`Missing --${key}`);
const config = JSON.parse(await readFile(values.config, 'utf8'));
const output = path.resolve(values.output);
await mkdir(output);
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
let session, current, receipt, turn = 0, closing = false;
let closePromise;
const initialAbort = new AbortController();

function answer(snapshot, runId) {
  const messages = snapshot.history.filter(item => item.run_id === runId && item.source === 'assistant'
    && item.content?.content?.kind === 'text' && item.content.content.text);
  return messages.at(-1)?.content.content.text ?? '';
}

async function continueTurn(text, directory) {
  const { api, identity, request, redact } = session;
  await mkdir(directory);
  const rawDirectory = path.join(session.privateDir, path.basename(directory));
  await mkdir(rawDirectory, { mode: 0o700 });
  const save = recordWriter(directory, rawDirectory, redact);
  const record = { startedAt: new Date().toISOString(), outcome: 'preparing', exportErrors: [], usageScope: 'session_cumulative' };
  try {
    const snapshot = await api('snapshot', identity);
    const key = randomUUID();
    const resolved = await api('source/resolve', { ...identity, resourceOwner: { kind: 'local' }, path: session.workdir });
    const prepared = await api('source/prepare', { ...identity, key: `${key}:source`, resourceOwner: resolved.resourceOwner,
      workspaceId: resolved.workspaceId, mode: 'live_root', content: { mode: 'saved-files' }, captureScopes: [] });
    receipt = await api('submit', { ...identity, key: `${key}:input`, expectedHead: snapshot.historyPage.head,
      text, source: prepared.source, model: session.config.model, agent: session.agent });
    record.receipt = receipt;
    record.outcome = 'accepted';
    await save('run.json', record);
    emit({ kind: 'accepted', turn: turn - 1, runId: receipt.run_id });
    if (closing) await api('run/cancel', { runId: receipt.run_id });
    while (true) {
      const run = await api('run', { runId: receipt.run_id });
      record.run = run;
      if (terminalRun(run.state)) { record.outcome = run.state; break; }
      await delay(500);
    }
  } catch (error) {
    record.outcome = 'error'; record.error = error.message;
  } finally {
    const snapshot = await exportNativeRecords({ api, request, identity, save, fromMs: session.startedAtMs, errors: record.exportErrors });
    if (snapshot?.runFailure) record.failure = snapshot.runFailure;
    record.finishedAt = new Date().toISOString();
    await save('run.json', record);
  }
  if (record.outcome !== 'completed' || record.exportErrors.length) throw new Error(`Native turn ended ${record.outcome}`);
  return record;
}

function submit(text) {
  if (current || closing) throw new Error('A native turn is already active or closing');
  const index = turn++;
  const directory = path.join(output, `turn-${String(index).padStart(3, '0')}`);
  const work = index === 0 ? runTrial({ runtime: values.runtime, config, instruction: text, workdir: values.workdir,
    output: directory, keepHost: true, signal: initialAbort.signal, onSession: value => {
      session = value; receipt = value.receipt;
      emit({ kind: 'accepted', turn: index, runId: receipt.run_id });
    } }) : continueTurn(text, directory);
  const tracked = work.then(async record => {
    const snapshot = JSON.parse(await readFile(path.join(directory, 'snapshot.json'), 'utf8'));
    emit({ kind: 'message', turn: index, content: answer(snapshot, record.receipt.run_id), outcome: record.outcome });
  }).catch(error => emit({ kind: 'failure', turn: index, message: session?.redact(error.message) ?? error.message }))
    .finally(() => { if (current === tracked) current = undefined; });
  current = tracked;
}

function close() { return closePromise ??= closeSession(); }

async function closeSession() {
  closing = true;
  if (session && receipt) {
    const state = await session.api('run', { runId: receipt.run_id });
    if (!terminalRun(state.state)) await session.api('run/cancel', { runId: receipt.run_id });
  } else if (current) initialAbort.abort();
  await current?.catch(() => undefined);
  if (!session) return { kind: 'closed', exportsComplete: false, reason: 'Native session was not admitted' };
  const directory = path.join(output, 'session');
  await mkdir(directory);
  const raw = path.join(session.privateDir, 'session');
  await mkdir(raw, { mode: 0o700 });
  const save = recordWriter(directory, raw, session.redact);
  const errors = [];
  await exportNativeRecords({ ...session, save, fromMs: session.startedAtMs, errors });
  const stopped = await session.stop();
  const runtime = JSON.parse(await readFile(path.join(values.runtime, 'bundle.json'), 'utf8'));
  const record = { model: session.config.model, runtime, outcome: 'session_closed', turns: turn,
    startedAt: new Date(session.startedAtMs).toISOString(), finishedAt: new Date().toISOString(),
    exportsComplete: errors.length === 0, exportErrors: errors, hostExit: stopped, usageScope: 'whole_session' };
  if (session.config.evaluationTransport) record.evaluationTransport = session.config.evaluationTransport;
  await save('run.json', record);
  return { kind: 'closed', exportsComplete: record.exportsComplete, hostExit: stopped };
}

const input = readline.createInterface({ input: process.stdin });
const handleSignal = () => {
  if (closing) return;
  void close().then(emit, error => {
    emit({ kind: 'failure', message: session?.redact(error.message) ?? error.message }); process.exitCode = 1;
  }).finally(() => input.close());
};
process.on('SIGTERM', handleSignal);
process.on('SIGINT', handleSignal);
try {
  for await (const line of input) {
    try {
      const command = JSON.parse(line);
      if (command.kind === 'submit') submit(command.text);
      else if (command.kind === 'close') { emit(await close()); break; }
      else throw new Error('Unknown session command');
    } catch (error) { emit({ kind: 'failure', message: session?.redact(error.message) ?? error.message }); }
  }
} finally {
  input.close();
  if (!closing) {
    try { emit(await close()); }
    catch (error) { emit({ kind: 'failure', message: session?.redact(error.message) ?? error.message }); process.exitCode = 1; }
  }
  process.off('SIGTERM', handleSignal);
  process.off('SIGINT', handleSignal);
}
