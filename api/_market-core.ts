import { timingSafeEqual } from 'node:crypto'

const SKILLS_SH_ORIGIN = 'https://skills.sh'

export type GatewayRequest = {
  method: string
  path: string[]
  query: URLSearchParams
  authorization?: string
}

export type GatewayResult = {
  status: number
  headers: Record<string, string>
  body: unknown
}

function json(status: number, body: unknown): GatewayResult {
  return {
    status,
    headers: {
      'Cache-Control': 'private, no-store',
      'Content-Type': 'application/json; charset=utf-8',
    },
    body,
  }
}

function authorized(provided: string | undefined, expected: string | undefined): boolean {
  if (!provided || !expected || !provided.startsWith('Bearer ')) return false
  const encoder = new TextEncoder()
  const supplied = encoder.encode(provided.slice('Bearer '.length))
  const configured = encoder.encode(expected)
  return supplied.length === configured.length && timingSafeEqual(supplied, configured)
}

function integerParam(query: URLSearchParams, name: string, minimum: number, maximum: number): string | null {
  const raw = query.get(name)
  if (raw === null) return null
  if (!/^\d+$/.test(raw)) throw new Error(`${name} must be an integer`)
  const value = Number(raw)
  if (value < minimum || value > maximum) throw new Error(`${name} is outside the supported range`)
  return String(value)
}

export function upstreamRequest(path: string[], query: URLSearchParams): { url: string } {
  if (!path.length || path[0] !== 'skills') throw new Error('unsupported marketplace route')

  const params = new URLSearchParams()
  if (path.length === 1) {
    const view = query.get('view') ?? 'all-time'
    if (!['all-time', 'trending', 'hot'].includes(view)) throw new Error('unsupported marketplace view')
    params.set('view', view)
    const page = integerParam(query, 'page', 0, 10000)
    const perPage = integerParam(query, 'per_page', 1, 500)
    if (page !== null) params.set('page', page)
    if (perPage !== null) params.set('per_page', perPage)
  } else if (path.length === 2 && path[1] === 'search') {
    const search = (query.get('q') ?? '').trim()
    if (search.length < 2 || search.length > 200) throw new Error('q must contain 2 to 200 characters')
    params.set('q', search)
    const limit = integerParam(query, 'limit', 1, 200)
    if (limit !== null) params.set('limit', limit)
  } else if (path.length === 2 && path[1] === 'curated') {
    // No query parameters are accepted by the curated endpoint.
  } else {
    if (
      path.length < 3
      || path.length > 5
      || path.slice(1).some((part) => part === '.' || part === '..' || !/^[A-Za-z0-9_.-]+$/.test(part))
    ) {
      throw new Error('invalid marketplace skill identifier')
    }
  }

  const suffix = path.map(encodeURIComponent).join('/')
  const queryString = params.toString()
  return {
    url: `${SKILLS_SH_ORIGIN}/api/v1/${suffix}${queryString ? `?${queryString}` : ''}`,
  }
}

export async function handleGatewayRequest(
  request: GatewayRequest,
  environment: Record<string, string | undefined>,
  fetchUpstream: typeof fetch = fetch,
  getOidcToken?: () => Promise<string>,
): Promise<GatewayResult> {
  if (request.method !== 'GET') return json(405, { error: 'method_not_allowed' })
  if (!authorized(request.authorization, environment.PGAGENT_MARKET_CLIENT_TOKEN)) {
    return json(401, { error: 'unauthorized' })
  }
  if (request.path.length === 1 && request.path[0] === 'status') {
    return json(200, { available: true, provider: 'skills.sh' })
  }

  let target: { url: string }
  try {
    target = upstreamRequest(request.path, request.query)
  } catch (error) {
    return json(400, { error: 'invalid_request', message: error instanceof Error ? error.message : 'invalid request' })
  }

  let oidcToken = environment.VERCEL_OIDC_TOKEN
  if (!oidcToken && getOidcToken) {
    try {
      oidcToken = await getOidcToken()
    } catch {
      return json(503, { error: 'oidc_unavailable' })
    }
  }
  if (!oidcToken) return json(503, { error: 'oidc_unavailable' })

  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const response = await fetchUpstream(target.url, {
        headers: { Accept: 'application/json', Authorization: `Bearer ${oidcToken}` },
        redirect: 'manual',
        signal: AbortSignal.timeout(10_000),
      })
      const contentType = response.headers.get('content-type') ?? ''
      const body = contentType.includes('application/json')
        ? await response.json()
        : { error: 'invalid_upstream_response' }
      if (response.status === 401) return json(502, { error: 'oidc_rejected' })
      return json(response.status, body)
    } catch (error) {
      console.error('skills.sh request failed', {
        attempt,
        error: error instanceof Error ? error.name : 'UnknownError',
      })
      if (attempt === 2) return json(502, { error: 'upstream_unreachable' })
    }
  }
  return json(502, { error: 'upstream_unreachable' })
}
