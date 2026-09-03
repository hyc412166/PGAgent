import assert from 'node:assert/strict'
import test from 'node:test'

import { handleGatewayRequest, upstreamRequest } from '../../api/_market-core.ts'

const secret = 'test-client-secret'

test('requires the configured client bearer token', async () => {
  const result = await handleGatewayRequest(
    { method: 'GET', path: ['status'], query: new URLSearchParams() },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret, VERCEL_OIDC_TOKEN: 'oidc' },
  )
  assert.equal(result.status, 401)
})

test('exposes status without contacting skills.sh after client authentication', async () => {
  const result = await handleGatewayRequest(
    { method: 'GET', path: ['status'], query: new URLSearchParams(), authorization: `Bearer ${secret}` },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret, VERCEL_OIDC_TOKEN: 'oidc' },
    async () => { throw new Error('must not fetch') },
  )
  assert.equal(result.status, 200)
  assert.deepEqual(result.body, { available: true, provider: 'skills.sh' })
})

test('does not resolve OIDC before authentication or for status', async () => {
  let oidcCalls = 0
  const resolveOidc = async () => {
    oidcCalls += 1
    throw new Error('OIDC unavailable')
  }
  const unauthorized = await handleGatewayRequest(
    { method: 'GET', path: ['skills'], query: new URLSearchParams() },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret },
    fetch,
    resolveOidc,
  )
  const status = await handleGatewayRequest(
    { method: 'GET', path: ['status'], query: new URLSearchParams(), authorization: `Bearer ${secret}` },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret },
    fetch,
    resolveOidc,
  )
  assert.equal(unauthorized.status, 401)
  assert.equal(status.status, 200)
  assert.equal(oidcCalls, 0)
})

test('maps OIDC resolution and rejection failures to stable gateway errors', async () => {
  const request = {
    method: 'GET',
    path: ['skills'],
    query: new URLSearchParams(),
    authorization: `Bearer ${secret}`,
  }
  const unavailable = await handleGatewayRequest(
    request,
    { PGAGENT_MARKET_CLIENT_TOKEN: secret },
    fetch,
    async () => { throw new Error('OIDC unavailable') },
  )
  const rejected = await handleGatewayRequest(
    request,
    { PGAGENT_MARKET_CLIENT_TOKEN: secret },
    async () => new Response(JSON.stringify({ error: 'unauthorized' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    }),
    async () => 'oidc-token',
  )
  assert.equal(unavailable.status, 503)
  assert.deepEqual(unavailable.body, { error: 'oidc_unavailable' })
  assert.equal(rejected.status, 502)
  assert.deepEqual(rejected.body, { error: 'oidc_rejected' })
})

test('allows only fixed marketplace routes and parameters', () => {
  assert.throws(() => upstreamRequest(['proxy', 'https://example.com'], new URLSearchParams()), /unsupported/)
  assert.throws(() => upstreamRequest(['skills', '..', 'secret'], new URLSearchParams()), /invalid/)
  assert.equal(
    upstreamRequest(['skills', 'search'], new URLSearchParams({ q: 'react agent', limit: '20' })).url,
    'https://skills.sh/api/v1/skills/search?q=react+agent&limit=20',
  )
})

test('preserves observable upstream errors', async () => {
  const result = await handleGatewayRequest(
    {
      method: 'GET',
      path: ['skills', 'search'],
      query: new URLSearchParams({ q: 'react' }),
      authorization: `Bearer ${secret}`,
    },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret, VERCEL_OIDC_TOKEN: 'oidc' },
    async () => new Response(JSON.stringify({ error: 'rate_limited' }), {
      status: 429,
      headers: { 'content-type': 'application/json' },
    }),
  )
  assert.equal(result.status, 429)
  assert.deepEqual(result.body, { error: 'rate_limited' })
  assert.equal(result.headers['Cache-Control'], 'private, no-store')
})

test('retries one transient upstream transport failure', async () => {
  let attempts = 0
  const result = await handleGatewayRequest(
    {
      method: 'GET',
      path: ['skills', 'search'],
      query: new URLSearchParams({ q: 'react' }),
      authorization: `Bearer ${secret}`,
    },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret, VERCEL_OIDC_TOKEN: 'oidc' },
    async () => {
      attempts += 1
      if (attempts === 1) throw new TypeError('transient network failure')
      return new Response(JSON.stringify({ data: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    },
  )
  assert.equal(result.status, 200)
  assert.equal(attempts, 2)
})
