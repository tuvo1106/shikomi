import { useState } from 'react'
import { api, HttpError } from '../api/client'

/**
 * "Resend the verification email" for a given address. Used wherever an
 * unverified user can end up: the post-signup screen, the Login page after a
 * failed sign-in, and the VerifyGate safety net. Unverified accounts can't log
 * in (DESIGN.md §4.1), so this is their only way to get a fresh link.
 *
 * The API answers every address identically (anti-enumeration), so success is
 * worded as "if there's an unverified account, it's on its way" — never as a
 * confirmation that the address is registered.
 */
export function ResendVerification({ email, label = 'Resend verification email' }: {
  email: string
  label?: string
}) {
  const [state, setState] = useState<'idle' | 'busy' | 'sent'>('idle')
  const [error, setError] = useState('')

  async function resend() {
    setError('')
    setState('busy')
    try {
      await api.post('/auth/resend-verification', { email })
      setState('sent')
    } catch (err) {
      // e.g. a 429: resend shares the per-IP auth rate limit with login/register.
      setError(err instanceof HttpError ? err.message : 'Could not send the email. Try again.')
      setState('idle')
    }
  }

  if (state === 'sent')
    return (
      <span className="text-sm text-emerald-400 light:text-emerald-600">
        If {email} has an unconfirmed account, a new link is on its way.
      </span>
    )

  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={resend}
        disabled={state === 'busy' || !email}
        className="w-full rounded-md border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
      >
        {state === 'busy' ? 'Sending…' : label}
      </button>
      {error && <p className="text-sm text-rose-400">{error}</p>}
    </div>
  )
}
