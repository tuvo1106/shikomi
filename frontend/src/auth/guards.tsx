/**
 * Route guards: wrappers that decide whether to render a protected page, redirect
 * to login, or show the email-verification wall. Guarding happens at the route
 * boundary so pages themselves can assume a valid, verified user. It waits for
 * `loading` before deciding, so a logged-in reload isn't bounced to /login while
 * the session is still restoring.
 */
import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { VerifyGate } from '../components/VerifyGate'
import { useAuth } from './session'
import type { User } from '../api/types'

/**
 * Whether the email-verification wall applies. Mirrors the backend's
 * `require_verified` dependency, which has no exemptions.
 */
function needsVerification(user: User) {
  return !user.email_verified
}

/** Gate a page on being signed in *and* email-verified; else redirect / show wall. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <div className="p-6 text-sm text-zinc-500">Loading…</div>
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />
  if (needsVerification(user)) return <VerifyGate />
  return <>{children}</>
}
