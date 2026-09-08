import assert from 'node:assert/strict'
import test from 'node:test'

// 网关契约测试：通过注入上游与 OIDC 获取器，验证鉴权、路由白名单、错误映射和重试。
import { handleGatewayRequest, upstreamRequest } from '../../api/_market-core.ts'

// 测试用客户端令牌；生产值只从部署环境读取。
const secret = 'test-client-secret'

// 验证缺少 Bearer 凭据时在任何上游访问前返回 401。
test('requires the configured client bearer token', async () => {
  const result = await handleGatewayRequest(
    { method: 'GET', path: ['status'], query: new URLSearchParams() },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret, VERCEL_OIDC_TOKEN: 'oidc' },
  )
  assert.equal(result.status, 401)
})

// 验证 status 是本地健康能力，鉴权后无需访问 skills.sh。
test('exposes status without contacting skills.sh after client authentication', async () => {
  const result = await handleGatewayRequest(
    { method: 'GET', path: ['status'], query: new URLSearchParams(), authorization: `Bearer ${secret}` },
    { PGAGENT_MARKET_CLIENT_TOKEN: secret, VERCEL_OIDC_TOKEN: 'oidc' },
    async () => { throw new Error('must not fetch') },
  )
  assert.equal(result.status, 200)
  assert.deepEqual(result.body, { available: true, provider: 'skills.sh' })
})

// oidcCalls 记录身份获取次数，证明未鉴权请求和 status 请求都不会提前申请部署令牌。
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

// 分别模拟 OIDC 获取失败和上游拒绝，确认它们映射为不同且稳定的错误码。
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

// 验证 path 与 query 的白名单边界，防止网关退化为任意地址代理。
test('allows only fixed marketplace routes and parameters', () => {
  assert.throws(() => upstreamRequest(['proxy', 'https://example.com'], new URLSearchParams()), /unsupported/)
  assert.throws(() => upstreamRequest(['skills', '..', 'secret'], new URLSearchParams()), /invalid/)
  assert.equal(
    upstreamRequest(['skills', 'search'], new URLSearchParams({ q: 'react agent', limit: '20' })).url,
    'https://skills.sh/api/v1/skills/search?q=react+agent&limit=20',
  )
})

// 上游正常返回的业务错误应保持状态与载荷，同时响应仍禁止缓存。
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

// attempts 记录传输尝试次数，验证首次网络异常后只重试一次并返回第二次结果。
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
