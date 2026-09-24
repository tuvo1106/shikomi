import { useState, type ReactNode } from 'react'
import { api, HttpError } from '../api/client'
import { useAuth } from '../auth/session'
import { Field } from '../components/AuthForm'
import { TwoFactor } from './settings/TwoFactor'

/** A titled settings section card. */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-5">
      <h2 className="mb-3 text-sm font-semibold text-zinc-200">{title}</h2>
      {children}
    </section>
  )
}

/** Read-only account details. */
function AccountSection() {
  const { user } = useAuth()
  if (!user) return null
  return (
    <Section title="Account">
      <dl className="space-y-2 text-sm">
        <div className="flex justify-between gap-4">
          <dt className="text-zinc-500">Username</dt>
          <dd className="text-zinc-200">{user.username}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-zinc-500">Email</dt>
          <dd className="truncate text-zinc-200">{user.email}</dd>
        </div>
      </dl>
    </Section>
  )
}

/** Change-password form (verifies the current password server-side). */
function PasswordSection() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setDone(false)
    if (next !== confirm) {
      setError('New passwords don’t match.')
      return
    }
    setBusy(true)
    try {
      await api.post('/auth/change-password', { current_password: current, new_password: next })
      setDone(true)
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (e) {
      setError(
        e instanceof HttpError && e.code === 'INVALID_CURRENT_PASSWORD'
          ? 'Your current password is incorrect.'
          : e instanceof HttpError
            ? e.message
            : 'Could not change password.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section title="Change password">
      <form onSubmit={submit} className="space-y-3">
        <Field label="Current password" type="password" value={current} onChange={setCurrent} />
        <Field label="New password" type="password" value={next} onChange={setNext} />
        <Field label="Confirm new password" type="password" value={confirm} onChange={setConfirm} />
        {error && <p className="text-sm text-rose-400 light:text-rose-600">{error}</p>}
        {done && (
          <p className="text-sm text-emerald-400 light:text-emerald-600">Password updated.</p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
        >
          {busy ? 'Updating…' : 'Update password'}
        </button>
      </form>
    </Section>
  )
}

/** Two-factor authentication: enrol, manage recovery codes, turn off. */
function TwoFactorSection() {
  const { user, refreshUser } = useAuth()
  if (!user) return null
  return (
    <Section title="Two-factor authentication">
      <TwoFactor enabled={user.totp_enabled} onChanged={refreshUser} />
    </Section>
  )
}

/**
 * Account settings: read-only account info, a change-password form, and
 * two-factor setup. Reachable from the user menu; gated behind `RequireAuth`.
 */
export default function Settings() {
  return (
    <div className="mx-auto max-w-lg space-y-4 p-6">
      <h1 className="text-xl font-bold text-zinc-100">Settings</h1>
      <AccountSection />
      <PasswordSection />
      <TwoFactorSection />
    </div>
  )
}
