import assert from 'node:assert/strict';
import { test } from 'node:test';
import { privateValueRedactor, resolveConfiguration } from '../varin_evals/configuration.mjs';

test('explicit endpoint binding resolves privately without changing the template', () => {
  const input = { models: { channels: [{ baseUrl: '${EVAL_ENDPOINT}', authentication: { kind: 'environment', variable: 'EVAL_SECRET' } }] } };
  const environment = { EVAL_ENDPOINT: 'https://provider.invalid/v1', EVAL_SECRET: 'fixture-' + 'private-key' };
  const resolved = resolveConfiguration(input, environment);
  assert.equal(input.models.channels[0].baseUrl, '${EVAL_ENDPOINT}');
  assert.equal(resolved.models.channels[0].baseUrl, environment.EVAL_ENDPOINT);
  const publicRecord = privateValueRedactor(resolved, environment)({
    request: { endpoint: environment.EVAL_ENDPOINT, error: 'Bearer ' + environment.EVAL_SECRET },
    note: 'Connected to https://provider.invalid/chat/completions', tokens: 12,
  });
  const serialized = JSON.stringify(publicRecord);
  assert.ok(!serialized.includes('provider.invalid'));
  assert.ok(!serialized.includes(environment.EVAL_SECRET));
  assert.equal(publicRecord.tokens, 12);
});

test('missing declared endpoint is an error before execution', () => {
  assert.throws(() => resolveConfiguration({ models: { channels: [{ baseUrl: '${NOT_CONFIGURED}' }] } }, {}), /Missing endpoint/);
});
