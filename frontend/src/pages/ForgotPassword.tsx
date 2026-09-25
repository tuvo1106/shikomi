import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, HttpError } from '../api/client'
import { AuthCard, Field, StatusCard } from '../components/AuthForm'

/** "Forgot password" page: request a reset link, then a check-your-email notice. */
export default function ForgotPassword() {
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await api.post('/auth/password-reset/request', { email })
      setSent(true)
    } catch (err) {
      setError(err instanceof HttpError ? err.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  if (sent)
    return (
      <StatusCard title="Check your email">
        If an account exists for <span className="text-zinc-200">{email}</span>, we’ve sent a link to
        reset your password.
        <div className="mt-4">
          <Link to="/login" className="text-indigo-400 hover:text-indigo-300">
            Back to sign in
          </Link>
        </div>
      </StatusCard>
    )

  return (
    <AuthCard
      title="Reset password"
      onSubmit={submit}
      error={error}
      busy={busy}
      footer={
        <Link to="/login" className="text-indigo-400 hover:text-indigo-300">
          Back to sign in
        </Link>
      }
    >
      <p className="text-sm text-zinc-400">Enter your email and we’ll send a reset link.</p>
      <Field label="Email" type="email" value={email} onChange={setEmail} />
    </AuthCard>
  )
}
