import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { User } from '../api/types'

// Control what the guards see by mocking the session hook.
const useAuth = vi.fn()
vi.mock('./session', () => ({ useAuth: () => useAuth() }))
// VerifyGate pulls in the API client; stub it to a recognisable marker.
vi.mock('../components/VerifyGate', () => ({ VerifyGate: () => <div>verify-wall</div> }))

import { RequireAuth } from './guards'

function user(overrides: Partial<User>): User {
  return {
    id: '1', email: 'x@example.com', username: 'x',
    email_verified: true, totp_enabled: false, ...overrides,
  }
}

function renderGuard(node: React.ReactNode) {
  render(<MemoryRouter><>{node}</></MemoryRouter>)
}

describe('RequireAuth verification wall', () => {
  it('walls an unverified user', () => {
    useAuth.mockReturnValue({ user: user({ email_verified: false }), loading: false })
    renderGuard(<RequireAuth><div>secret</div></RequireAuth>)
    expect(screen.getByText('verify-wall')).toBeInTheDocument()
    expect(screen.queryByText('secret')).not.toBeInTheDocument()
  })

  it('lets a verified user through', () => {
    useAuth.mockReturnValue({ user: user({}), loading: false })
    renderGuard(<RequireAuth><div>secret</div></RequireAuth>)
    expect(screen.getByText('secret')).toBeInTheDocument()
    expect(screen.queryByText('verify-wall')).not.toBeInTheDocument()
  })
})
