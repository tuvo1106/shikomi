/**
 * Auth session state for the whole app, exposed via React context.
 *
 * Holds the current `user` and the login/register/logout/refresh actions. It's the
 * bridge between the low-level token plumbing in `api/client.ts` and the UI: the
 * client owns the *access token*, this provider owns the *user object* that
 * components render and route guards check.
 */
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, refreshSession, setAccessToken } from '../api/client'
import type { User } from '../api/types'

/** What a password login can lead to: a session, or a second step for 2FA accounts. */
export type LoginResult = { status: 'signed_in' } | { status: 'mfa_required'; mfaToken: string }

type AuthState = {
  user: User | null
  loading: boolean
  login: (email: string, password: string, remember: boolean) => Promise<LoginResult>
  /** Finish a two-factor login: redeem the challenge from `login` with a code. */
  completeMfa: (mfaToken: string, code: string, remember: boolean) => Promise<void>
  register: (email: string, username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

/**
 * Provides auth state to the tree. On mount it bootstraps: try to restore a
 * session from the refresh cookie, then load the user — so a logged-in reload
 * lands authenticated with no flash of the signed-out UI. `loading` is true until
 * that resolves, which route guards use to avoid redirecting prematurely.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // The access token is in memory only. Restore a session from the refresh
    // cookie first, then load the user — so a logged-in reload makes no 401.
    async function bootstrap() {
      try {
        if (await refreshSession()) {
          setUser(await api.get<User>('/auth/me'))
        }
      } catch {
        setUser(null)
      } finally {
        setLoading(false)
      }
    }
    bootstrap()
  }, [])

  async function login(email: string, password: string, remember: boolean): Promise<LoginResult> {
    const res = await api.post<
      { access_token: string; user: User } | { mfa_required: true; mfa_token: string }
    >('/auth/login', { email, password, remember })
    // A 2FA account gets a challenge instead of a session: the password alone signs
    // nobody in, so there's no token or user to store yet (DESIGN.md §4.1).
    if ('mfa_required' in res) return { status: 'mfa_required', mfaToken: res.mfa_token }
    setAccessToken(res.access_token)
    setUser(res.user)
    return { status: 'signed_in' }
  }

  async function completeMfa(mfaToken: string, code: string, remember: boolean) {
    const res = await api.post<{ access_token: string; user: User }>('/auth/login/2fa', {
      mfa_token: mfaToken,
      code,
      remember,
    })
    setAccessToken(res.access_token)
    setUser(res.user)
  }

  /**
   * Start a signup. Deliberately does *not* log in: the account can't sign in
   * until its email is verified, and the API answers a new and an
   * already-registered email identically (anti-enumeration, DESIGN.md §4.1), so
   * the only next step is the inbox.
   */
  async function register(email: string, username: string, password: string) {
    await api.post('/auth/register', { email, username, password })
  }

  async function logout() {
    await api.post('/auth/logout').catch(() => {})
    setAccessToken(null)
    setUser(null)
  }

  async function refreshUser() {
    setUser(await api.get<User>('/auth/me'))
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, completeMfa, register, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  )
}

/**
 * Access the auth context. Throws if used outside `<AuthProvider>` — a loud
 * failure that catches a missing-provider mistake at first render, not later.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
