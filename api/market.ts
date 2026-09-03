import { getVercelOidcToken } from '@vercel/oidc'

import { handleGatewayRequest } from './_market-core.js'

export default {
  async fetch(request: Request): Promise<Response> {
    const requestUrl = new URL(request.url)
    const rawPath = requestUrl.searchParams.get('path') ?? ''
    requestUrl.searchParams.delete('path')
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
