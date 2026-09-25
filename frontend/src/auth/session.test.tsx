import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AuthProvider, useAuth } from './session'
import { json } from '../test/http'

function Consumer() {
  const { user, loading, login, logout } = useAuth()
  if (loading) return <div>loading</div>
  return (
    <div>
      <span>{user ? user.username : 'anon'}</span>
      <button onClick={() => login('a@b.com', 'pw', true)}>login</button>
      <button onClick={() => logout()}>logout</button>
    </div>
  )
}

const USER = { id: '1', email: 'a@b.com', username: 'alice', email_verified: true }

function renderApp() {
  render(
    <AuthProvider>
      <Consumer />
    </AuthProvider>,
  )
}

describe('AuthProvider', () => {
  it('restores the session when the refresh cookie is valid', async () => {
    document.cookie = 'has_session=1; path=/' // the readable hint the server sets on login
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string) => {
        const url = String(input)
        if (url.includes('/auth/refresh')) return Promise.resolve(json({ access_token: 't' }))
        if (url.includes('/auth/me')) return Promise.resolve(json(USER))
        return Promise.resolve(json({}, 404))
      }),
    )
    renderApp()
    expect(await screen.findByText('alice')).toBeInTheDocument()
  })

  it('shows anonymous when there is no session', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(json({ detail: 'no', code: 'NO_REFRESH' }, 401))),
    )
    renderApp()
    expect(await screen.findByText('anon')).toBeInTheDocument()
  })

  it('logs in and out', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/auth/refresh')) return Promise.resolve(json({ detail: 'no' }, 401))
        if (url.includes('/auth/login')) return Promise.resolve(json({ access_token: 't', user: USER }))
        if (url.includes('/auth/logout')) return Promise.resolve(new Response(null, { status: 204 }))
        void init
        return Promise.resolve(json({}, 404))
      }),
    )
    renderApp()
    expect(await screen.findByText('anon')).toBeInTheDocument()

    await userEvent.click(screen.getByText('login'))
    expect(await screen.findByText('alice')).toBeInTheDocument()

    await userEvent.click(screen.getByText('logout'))
    expect(await screen.findByText('anon')).toBeInTheDocument()
  })
})
