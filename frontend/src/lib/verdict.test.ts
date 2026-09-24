import { describe, expect, it } from 'vitest'
import { nextPollDelay, POLL_DEADLINE_MS, POLL_INTERVAL_MS } from './verdict'

describe('nextPollDelay', () => {
  it('keeps polling a judging submission every second', () => {
    expect(nextPollDelay('pending', 0, 5_000)).toBe(POLL_INTERVAL_MS)
    expect(nextPollDelay('running', 0, POLL_DEADLINE_MS - 1)).toBe(POLL_INTERVAL_MS)
  })

  it('stops at the deadline, so a submission the server never settles cannot poll forever', () => {
    expect(nextPollDelay('running', 0, POLL_DEADLINE_MS)).toBe(false)
    expect(nextPollDelay('pending', 1_000, 1_000 + POLL_DEADLINE_MS + 5)).toBe(false)
  })

  it('measures the deadline from when polling started, not from the epoch', () => {
    const start = 1_700_000_000_000
    expect(nextPollDelay('running', start, start + 10_000)).toBe(POLL_INTERVAL_MS)
  })

  it('stops on a terminal status and before anything has been fetched', () => {
    expect(nextPollDelay('accepted', 0, 1)).toBe(false)
    expect(nextPollDelay('judge_error', 0, 1)).toBe(false)
    expect(nextPollDelay(undefined, 0, 1)).toBe(false)
  })

  it('outlasts the server-side sweep, so a healthy backend always answers first', () => {
    // 5 min stale threshold + up to a minute for the once-a-minute cron.
    expect(POLL_DEADLINE_MS).toBeGreaterThan(6 * 60 * 1000)
  })
})
