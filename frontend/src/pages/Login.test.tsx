import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../auth/session'
import Login from './Login'
import { json } from '../test/http'

const USER = {
  id: '1', email: 'a@b.com', username: 'alice',
  email_verified: true, totp_enabled: true,
}

/** Stub the API: no session on load, a 2FA challenge on password login, `second` for the code step. */
function stubApi(second: () => Response) {
  const fn = vi.fn((input: string) => {
    const url = String(input)
    if (url.includes('/auth/refresh')) return Promise.resolve(json({ detail: 'no' }, 401))
    if (url.includes('/auth/login/2fa')) return Promise.resolve(second())
    if (url.includes('/auth/login')) return Promise.resolve(json({ mfa_required: true, mfa_token: 'mfa-jwt' }))
    return Promise.resolve(json({}, 404))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

async function signInToCodeStep() {
  const user = userEvent.setup()
  render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/problems" element={<div>problem list</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
  await user.type(await screen.findByLabelText('Email'), 'a@b.com')
  await user.type(screen.getByLabelText('Password'), 'pw-secret')
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
  await screen.findByRole('heading', { name: 'Two-factor authentication' })
  return user
}

afterEach(() => vi.unstubAllGlobals())

describe('Login with two-factor auth', () => {
  it('asks for a code after the password, then signs in', async () => {
    const fetchMock = stubApi(() => json({ access_token: 't', user: USER }))
    const user = await signInToCodeStep()

    await user.type(screen.getByLabelText('Authentication code'), '123456')
    await user.click(screen.getByRole('button', { name: 'Two-factor authentication' }))

    expect(await screen.findByText('problem list')).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([u]) => String(u).includes('/auth/login/2fa')) as unknown as [string, RequestInit]
    expect(JSON.parse(String(call[1].body))).toMatchObject({ mfa_token: 'mfa-jwt', code: '123456' })
  })

  it('shows a wrong code and stays on the code step', async () => {
    stubApi(() => json({ detail: 'That code is not valid.', code: 'INVALID_CODE' }, 400))
    const user = await signInToCodeStep()

    await user.type(screen.getByLabelText('Authentication code'), '000000')
    await user.click(screen.getByRole('button', { name: 'Two-factor authentication' }))

    expect(await screen.findByText('That code is not valid.')).toBeInTheDocument()
    expect(screen.getByLabelText('Authentication code')).toBeInTheDocument()
  })

  it('falls back to the password step when the challenge has expired', async () => {
    stubApi(() => json({ detail: 'Sign-in expired.', code: 'INVALID_MFA_TOKEN' }, 401))
    const user = await signInToCodeStep()

    await user.type(screen.getByLabelText('Authentication code'), '123456')
    await user.click(screen.getByRole('button', { name: 'Two-factor authentication' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByText('Sign-in expired.')).toBeInTheDocument()
  })
})
