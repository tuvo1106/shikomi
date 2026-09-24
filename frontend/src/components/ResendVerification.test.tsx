import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ResendVerification } from './ResendVerification'
import { json } from '../test/http'

describe('ResendVerification', () => {
  it('posts the email and confirms without claiming the account exists', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(json({ message: 'ok' }, 202)))
    vi.stubGlobal('fetch', fetchMock)
    render(<ResendVerification email="a@b.com" />)

    await userEvent.click(screen.getByRole('button', { name: /Resend/i }))

    expect(await screen.findByText(/If a@b.com has an unconfirmed account/)).toBeInTheDocument()
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toContain('/auth/resend-verification')
    expect(JSON.parse(String(init.body))).toEqual({ email: 'a@b.com' })
  })

  it('shows the error and lets you retry when the request fails (e.g. rate limited)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(json({ detail: 'Too many requests.', code: 'RATE_LIMITED' }, 429))),
    )
    render(<ResendVerification email="a@b.com" />)

    await userEvent.click(screen.getByRole('button', { name: /Resend/i }))

    expect(await screen.findByText('Too many requests.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Resend/i })).toBeEnabled()
  })
})
