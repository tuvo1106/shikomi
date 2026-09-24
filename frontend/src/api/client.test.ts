import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, getAccessToken, HttpError, setAccessToken } from './client'
import { json } from '../test/http'

beforeEach(() => setAccessToken(null))

describe('api client', () => {
  it('returns parsed JSON on success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ok: 1 })))
    expect(await api.get('/x')).toEqual({ ok: 1 })
  })

  it('sends the bearer token when set', async () => {
    setAccessToken('abc')
    const fetchMock = vi.fn().mockResolvedValue(json({ ok: 1 }))
    vi.stubGlobal('fetch', fetchMock)
    await api.get('/x')
    const headers = fetchMock.mock.calls[0][1].headers
    expect(headers.Authorization).toBe('Bearer abc')
  })

  it('on 401 refreshes then retries the original request', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ detail: 'no', code: 'INVALID_TOKEN' }, 401)) // GET /x
      .mockResolvedValueOnce(json({ access_token: 'fresh' })) // POST /auth/refresh
      .mockResolvedValueOnce(json({ ok: 1 })) // retry GET /x
    vi.stubGlobal('fetch', fetchMock)

    expect(await api.get('/x')).toEqual({ ok: 1 })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(getAccessToken()).toBe('fresh')
  })

  it('throws HttpError with code when refresh also fails', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ detail: 'no', code: 'INVALID_TOKEN' }, 401))
      .mockResolvedValueOnce(json({ detail: 'no', code: 'NO_REFRESH' }, 401))
    vi.stubGlobal('fetch', fetchMock)

    const err = (await api.get('/x').catch((e) => e)) as HttpError
    expect(err).toBeInstanceOf(HttpError)
    expect(err.code).toBe('INVALID_TOKEN')
    expect(err.status).toBe(401)
  })

  it('dedupes concurrent refreshes into a single call', async () => {
    let refreshCalls = 0
    const fetchMock = vi.fn((input: string) => {
      const url = String(input)
      if (url.includes('/auth/refresh')) {
        refreshCalls += 1
        return Promise.resolve(json({ access_token: 'fresh' }))
      }
      return Promise.resolve(
        getAccessToken() ? json({ ok: 1 }) : json({ detail: 'no', code: 'INVALID_TOKEN' }, 401),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const [a, b] = await Promise.all([api.get('/a'), api.get('/b')])
    expect(a).toEqual({ ok: 1 })
    expect(b).toEqual({ ok: 1 })
    expect(refreshCalls).toBe(1) // both 401s shared one refresh
  })
})
