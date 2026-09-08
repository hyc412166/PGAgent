// Skill 市场网关核心：验证客户端凭据、限制上游路由，并用部署身份访问 skills.sh。
// market.ts 负责平台适配；此文件通过可注入 fetch/OIDC 获取器供离线测试验证。
import { timingSafeEqual } from 'node:crypto'

// 固定上游来源，不允许客户端把网关用作任意 URL 代理。
const SKILLS_SH_ORIGIN = 'https://skills.sh'

export type GatewayRequest = {
  // method 为 HTTP 方法；path 为拆分后的路由；query 为用户查询；authorization 为客户端凭据。
  method: string
  path: string[]
  query: URLSearchParams
  authorization?: string
}

export type GatewayResult = {
  // status/headers/body 分别由平台入口映射为响应状态、响应头和 JSON 内容。
  status: number
  headers: Record<string, string>
  body: unknown
}

// 把状态码 status 与载荷 body 包装为统一响应，禁止共享缓存保存鉴权后的市场数据。
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

// provided 是请求头，expected 是部署配置；先检查长度，再用恒定时间比较验证令牌。
function authorized(provided: string | undefined, expected: string | undefined): boolean {
  if (!provided || !expected || !provided.startsWith('Bearer ')) return false
  // encoder 转成字节；supplied 去除 Bearer 前缀，configured 是服务端期望的字节序列。
  const encoder = new TextEncoder()
  const supplied = encoder.encode(provided.slice('Bearer '.length))
  const configured = encoder.encode(expected)
  return supplied.length === configured.length && timingSafeEqual(supplied, configured)
}

// 从 query 读取 name 指定的整数，minimum/maximum 为闭区间；缺省返回 null，非法输入抛错。
function integerParam(query: URLSearchParams, name: string, minimum: number, maximum: number): string | null {
  // raw 保留原始输入以拒绝小数和符号；value 用于范围比较，最终返回规范十进制文本。
  const raw = query.get(name)
  if (raw === null) return null
  if (!/^\d+$/.test(raw)) throw new Error(`${name} must be an integer`)
  const value = Number(raw)
  if (value < minimum || value > maximum) throw new Error(`${name} is outside the supported range`)
  return String(value)
}

// 将受支持的 path 与 query 映射到固定上游 URL；异常交由请求处理器转为 400。
export function upstreamRequest(path: string[], query: URLSearchParams): { url: string } {
  if (!path.length || path[0] !== 'skills') throw new Error('unsupported marketplace route')

  // params 只收集白名单参数，客户端额外参数不会透传。
  const params = new URLSearchParams()
  if (path.length === 1) {
    // view 选择榜单；page 是从零开始的页号，perPage 是单页条数。
    const view = query.get('view') ?? 'all-time'
    if (!['all-time', 'trending', 'hot'].includes(view)) throw new Error('unsupported marketplace view')
    params.set('view', view)
    const page = integerParam(query, 'page', 0, 10000)
    const perPage = integerParam(query, 'per_page', 1, 500)
    if (page !== null) params.set('page', page)
    if (perPage !== null) params.set('per_page', perPage)
  } else if (path.length === 2 && path[1] === 'search') {
    // search 是去掉首尾空白的关键词；limit 控制最多返回多少条搜索结果。
    const search = (query.get('q') ?? '').trim()
    if (search.length < 2 || search.length > 200) throw new Error('q must contain 2 to 200 characters')
    params.set('q', search)
    const limit = integerParam(query, 'limit', 1, 200)
    if (limit !== null) params.set('limit', limit)
  } else if (path.length === 2 && path[1] === 'curated') {
    // 精选列表不接受查询参数。
  } else {
    if (
      path.length < 3
      || path.length > 5
      || path.slice(1).some((part) => part === '.' || part === '..' || !/^[A-Za-z0-9_.-]+$/.test(part))
    ) {
      throw new Error('invalid marketplace skill identifier')
    }
  }

  // suffix 对每段标识单独编码；queryString 是过滤后的查询串，空串时不附加问号。
  const suffix = path.map(encodeURIComponent).join('/')
  const queryString = params.toString()
  return {
    url: `${SKILLS_SH_ORIGIN}/api/v1/${suffix}${queryString ? `?${queryString}` : ''}`,
  }
}

// 请求链路：客户端鉴权 → 路由验证 → 获取上游 OIDC → 请求与一次传输重试 → 响应映射。
// request 是标准化请求，environment 提供凭据；fetchUpstream/getOidcToken 可由平台或测试注入。
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

  // target 是验证后的唯一上游地址；路由错误不会触发令牌获取或网络访问。
  let target: { url: string }
  try {
    target = upstreamRequest(request.path, request.query)
  } catch (error) {
    return json(400, { error: 'invalid_request', message: error instanceof Error ? error.message : 'invalid request' })
  }

  // oidcToken 是部署访问上游的身份，与用户访问本网关的客户端令牌不同。
  let oidcToken = environment.VERCEL_OIDC_TOKEN
  if (!oidcToken && getOidcToken) {
    try {
      oidcToken = await getOidcToken()
    } catch {
      return json(503, { error: 'oidc_unavailable' })
    }
  }
  if (!oidcToken) return json(503, { error: 'oidc_unavailable' })

  // attempt 只控制抛异常的请求重试；上游 HTTP 错误直接返回，不在此重试。
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      // response 是上游响应；手动处理重定向，避免令牌被自动带往其他地址。
      const response = await fetchUpstream(target.url, {
        headers: { Accept: 'application/json', Authorization: `Bearer ${oidcToken}` },
        redirect: 'manual',
        signal: AbortSignal.timeout(10_000),
      })
      // contentType 决定是否解析 JSON；body 保留上游错误内容，非 JSON 用明确错误载荷替代。
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
