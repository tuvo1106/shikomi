import { useAuth } from '../auth/session'
import { StatusCard } from './AuthForm'
import { ResendVerification } from './ResendVerification'

/**
 * The full-screen wall shown by the route guards to signed-in but unverified
 * users. Unverified accounts can no longer log in (DESIGN.md §4.1), so this is
 * only reachable from a session that predates that rule — kept as a safety net
 * rather than trusting that no such session exists.
 * Offers Resend, "I've verified — continue" (re-fetches the user so the gate
 * clears once verified in another tab), and Sign out.
 */
export function VerifyGate() {
  const { user, logout, refreshUser } = useAuth()

  return (
    <StatusCard title="Verify your email">
      We sent a verification link to <span className="text-zinc-200">{user?.email}</span>. Confirm it
      to start using shikomi.
      <div className="mt-5 flex flex-col gap-2">
        <ResendVerification email={user?.email ?? ''} />
        <button
          onClick={() => refreshUser()}
          className="rounded-md border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800"
        >
          I’ve verified — continue
        </button>
        <button onClick={() => logout()} className="text-sm text-zinc-500 hover:text-zinc-300">
          Sign out
        </button>
      </div>
    </StatusCard>
  )
}
