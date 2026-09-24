import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/session'
import { HttpError } from '../api/client'
import { AuthCard, Field, StatusCard } from '../components/AuthForm'
import { ResendVerification } from '../components/ResendVerification'

/**
 * Sign-up page. A successful submit always lands on "check your email": the
 * API answers a new and an already-registered address identically, and the
 * account can't sign in until the emailed link is followed (DESIGN.md §4.1). So
 * the confirmation is worded to be true either way, and offers a resend.
 */
export default function Register() {
  const { register } = useAuth()
  const [email, setEmail] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await register(email, username, password)
      setSubmitted(true)
    } catch (err) {
      setError(err instanceof HttpError ? err.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  if (submitted)
    return (
      <StatusCard title="Check your email">
        We’ve sent a link to <span className="text-zinc-200">{email}</span>. Follow it to finish
        signing up, then sign in.
        <div className="mt-5 flex flex-col gap-2">
          <ResendVerification email={email} label="Didn’t get it? Resend" />
          <Link to="/login" className="text-sm text-indigo-400 hover:text-indigo-300">
            Go to sign in
          </Link>
        </div>
      </StatusCard>
    )

  return (
    <AuthCard
      title="Register"
      onSubmit={submit}
      error={error}
      busy={busy}
      footer={
        <>
          Have an account?{' '}
          <Link to="/login" className="text-indigo-400 hover:text-indigo-300">
            Sign in
          </Link>
        </>
      }
    >
      <Field label="Email" type="email" value={email} onChange={setEmail} />
      <Field label="Username" value={username} onChange={setUsername} />
      <Field label="Password (min 8)" type="password" value={password} onChange={setPassword} />
    </AuthCard>
  )
}
