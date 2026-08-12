import { describe, expect, it } from 'vitest'

import { usageDatePresetBounds, usageDateRange } from './usageDateRange'

describe('usage date ranges', () => {
  const anchor = new Date(2026, 7, 12, 16, 30, 0, 0)

  it('keeps today and the rolling 24-hour range distinct', () => {
    const today = usageDatePresetBounds('today', anchor)
    const last24Hours = usageDatePresetBounds('24h', anchor)

    expect(today.start.getTime()).toBe(new Date(2026, 7, 12, 0, 0, 0, 0).getTime())
    expect(last24Hours.start.getTime()).toBe(anchor.getTime() - 24 * 60 * 60 * 1000)
    expect(last24Hours.start.getTime()).not.toBe(today.start.getTime())
  })

  it('serializes a rolling 24-hour range instead of the current calendar day', () => {
    const params = new URLSearchParams(usageDateRange('2026-08-11', '2026-08-12', '24h', anchor).slice(1))

    expect(params.get('start_at')).toBe(new Date(anchor.getTime() - 24 * 60 * 60 * 1000).toISOString())
    expect(params.get('end_at')).toBe(anchor.toISOString())
  })
})
