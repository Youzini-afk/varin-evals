// Shared observation and shutdown of Varin's original Host APIs.
import { writeFile } from 'node:fs/promises';
import path from 'node:path';

export const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
export const terminalRun = state => ['completed', 'failed', 'cancelled'].includes(state);

export function recordWriter(output, rawDirectory, redact) {
  return async (name, value) => {
    await writeFile(path.join(rawDirectory, name), JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
    await writeFile(path.join(output, name), JSON.stringify(redact(value), null, 2) + '\n');
  };
}

export async function exportNativeRecords({ api, request, identity, save, fromMs, errors }) {
  const collect = async (name, read) => {
    try { const result = await read(); await save(name, result); return result; }
    catch (error) { errors.push({ file: name, error: error.message }); }
  };
  const snapshot = await collect('snapshot.json', () => api('snapshot', identity));
  if (snapshot) await collect('conversation.json', async () => (await request('/api/threads/export', { ...identity,
    headId: snapshot.historyPage.head, format: 'json', includeTools: true, includeAttachments: false })).json());
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
    fromMs, toMs: Date.now() + 1, utcOffsetMinutes: 0,
  })).json());
  return snapshot;
}

export async function stopNativeHost({ host, hostExited, request }) {
  try { await request('/api/system/shutdown', {}); } catch { /* The process receipt below confirms exit. */ }
  const waitForExit = async () => {
    let timer;
    try { return await Promise.race([hostExited, new Promise(resolve => { timer = setTimeout(() => resolve(null), 10_000); })]); }
    finally { clearTimeout(timer); }
  };
  let stopped = await waitForExit();
  if (!stopped) {
    try { process.kill(-host.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    stopped = await waitForExit();
  }
  if (!stopped) {
    try { process.kill(-host.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    stopped = await hostExited;
  }
  return stopped;
}
