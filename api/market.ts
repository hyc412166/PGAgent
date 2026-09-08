// Vercel HTTP 入口：把重写后的路径、客户端凭据交给市场核心，再还原为 Web Response。
import { getVercelOidcToken } from '@vercel/oidc'

import { handleGatewayRequest } from './_market-core.js'

export default {
  // request 是平台收到的请求；fetchUpstream 与 OIDC 获取器由核心按鉴权结果调用。
  async fetch(request: Request): Promise<Response> {
    // requestUrl 保留查询条件；rawPath 来自 vercel.json 的 path 重写，不转发给上游查询参数。
    const requestUrl = new URL(request.url)
    const rawPath = requestUrl.searchParams.get('path') ?? ''
    requestUrl.searchParams.delete('path')
    // result 包含核心决定的状态码、禁缓存响应头和 JSON 响应体。
    const result = await handleGatewayRequest(
      {
        method: request.method,
        path: rawPath.split('/').filter(Boolean),
        query: requestUrl.searchParams,
        authorization: request.headers.get('authorization') ?? undefined,
      },
      {
        PGAGENT_MARKET_CLIENT_TOKEN: process.env.PGAGENT_MARKET_CLIENT_TOKEN,
      },
      fetch,
      getVercelOidcToken,
    )
    return Response.json(result.body, { status: result.status, headers: result.headers })
  },
}
