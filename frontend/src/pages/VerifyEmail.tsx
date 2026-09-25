import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, HttpError } from '../api/client'
import { useAuth } from '../auth/session'
import { StatusCard } from '../components/AuthForm'

/**
 * Landing page for the emailed verification link (`/verify-email?token=…`). POSTs
 * the token on mount and shows loading/success/failure. The `ran` ref guards
 * against React StrictMode's double-invoke in dev — tokens are single-use, so a
 * second call would spuriously fail with "already used".
 */
export default function VerifyEmail() {
  const [params] = useSearchParams()
  const token = params.get('token') ?? ''
  const { user, refreshUser } = useAuth()
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading')
  const [message, setMessage] = useState('')
  const ran = useRef(false)

  useEffect(() => {
    if (ran.current) return // guard StrictMode's double-invoke (token is single-use)
    ran.current = true
    if (!token) {
      setState('error')
      setMessage('This verification link is missing its token.')
      return
    }
    api
      .post('/auth/verify-email', { token })
      .then(() => {
        setState('ok')
        if (user) refreshUser().catch(() => {})
      })
      .catch((e) => {
        setState('error')
        setMessage(e instanceof HttpError ? e.message : 'Verification failed.')
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (state === 'loading') return <StatusCard title="Verifying…">Confirming your email address.</StatusCard>

  if (state === 'ok')
    return (
      <StatusCard title="Email verified">
        Your email is confirmed.
        <div className="mt-4">
          <Link to={user ? '/problems' : '/login'} className="text-indigo-400 hover:text-indigo-300">
            {user ? 'Go to problems' : 'Sign in'}
          </Link>
        </div>
      </StatusCard>
    )

  return (
    <StatusCard title="Verification failed">
      {message}
      <div className="mt-4">
        <Link to={user ? '/problems' : '/login'} className="text-indigo-400 hover:text-indigo-300">
          {user ? 'Back to problems' : 'Sign in'}
        </Link>
      </div>
    </StatusCard>
  )
}
