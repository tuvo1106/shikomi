import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/session'
import { HttpError } from '../api/client'
import { AuthCard, Field } from '../components/AuthForm'
import { ResendVerification } from '../components/ResendVerification'

/**
 * Sign-in page. On success sets the session and routes to /problems. An account with
 * two-factor auth gets a second step after its password: the code screen below, which
 * redeems the challenge the server issued (a code from the authenticator app, or a
 * recovery code).
 */
export default function Login() {
  const { login, completeMfa } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(true)
  const [error, setError] = useState('')
  // Set once the password checked out but a second factor is owed.
  const [mfaToken, setMfaToken] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  // A failed sign-in may be an account that isn't confirmed yet — the API can't
  // say which (anti-enumeration), so offer a resend after any credentials error.
  const [offerResend, setOfferResend] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setOfferResend(false)
    setBusy(true)
    try {
      const result = await login(email, password, remember)
      if (result.status === 'mfa_required') setMfaToken(result.mfaToken)
      else navigate('/problems')
    } catch (err) {
      setError(err instanceof HttpError ? err.message : 'Something went wrong.')
      setOfferResend(err instanceof HttpError && err.code === 'INVALID_CREDENTIALS')
    } finally {
      setBusy(false)
    }
  }

  async function submitCode(e: FormEvent) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await completeMfa(mfaToken!, code.trim(), remember)
      navigate('/problems')
    } catch (err) {
      if (err instanceof HttpError && err.code === 'INVALID_MFA_TOKEN') {
        // The challenge expired (a few minutes) or was invalidated: start over.
        setMfaToken(null)
        setCode('')
      }
      setError(err instanceof HttpError ? err.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  if (mfaToken) {
    return (
      <AuthCard
        title="Two-factor authentication"
        onSubmit={submitCode}
        error={error}
        busy={busy}
        footer={
          <button
            type="button"
            onClick={() => {
              setMfaToken(null)
              setCode('')
              setError('')
            }}
            className="text-indigo-400 hover:text-indigo-300"
          >
            Use a different account
          </button>
        }
      >
        <p className="text-sm text-zinc-400">
          Enter the 6-digit code from your authenticator app, or one of your recovery codes.
        </p>
        <Field
          label="Authentication code"
          value={code}
          onChange={setCode}
          autoComplete="one-time-code"
          autoFocus
        />
      </AuthCard>
    )
  }

  return (
    <AuthCard
      title="Sign in"
      onSubmit={submit}
      error={error}
      busy={busy}
      footer={
        <>
          No account?{' '}
          <Link to="/register" className="text-indigo-400 hover:text-indigo-300">
            Register
          </Link>
        </>
      }
    >
      <Field label="Email" type="email" value={email} onChange={setEmail} />
      <Field label="Password" type="password" value={password} onChange={setPassword} />
      <div className="text-right text-sm">
        <Link to="/forgot-password" className="text-indigo-400 hover:text-indigo-300">
          Forgot password?
        </Link>
      </div>
      <label className="flex items-center gap-2 text-sm text-zinc-400">
        <input
          type="checkbox"
          checked={remember}
          onChange={(e) => setRemember(e.target.checked)}
          className="accent-indigo-500"
        />
        Keep me signed in
      </label>
      {offerResend && (
        <ResendVerification email={email} label="Didn’t get your confirmation email? Resend it" />
      )}
    </AuthCard>
  )
}
