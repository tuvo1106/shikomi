import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, HttpError } from '../api/client'
import { AuthCard, Field, StatusCard } from '../components/AuthForm'

/** Reset-password page reached from the emailed link (`?token=…`): set a new password. */
export default function ResetPassword() {
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await api.post('/auth/password-reset/confirm', { token, password })
      setDone(true)
    } catch (err) {
      setError(err instanceof HttpError ? err.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  if (!token)
    return (
      <StatusCard title="Invalid link">
        This password reset link is missing its token.
        <div className="mt-4">
          <Link to="/forgot-password" className="text-indigo-400 hover:text-indigo-300">
            Request a new link
          </Link>
        </div>
      </StatusCard>
    )

  if (done)
    return (
      <StatusCard title="Password reset">
        Your password has been updated.
        <div className="mt-4">
          <Link to="/login" className="text-indigo-400 hover:text-indigo-300">
            Sign in
          </Link>
        </div>
      </StatusCard>
    )

  return (
    <AuthCard title="Choose a new password" onSubmit={submit} error={error} busy={busy}>
      <Field
        label="New password (min 8)"
        type="password"
        value={password}
        onChange={setPassword}
      />
    </AuthCard>
  )
}
