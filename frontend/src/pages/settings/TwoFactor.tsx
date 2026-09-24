import { useState, type FormEvent } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { api, HttpError } from '../../api/client'
import { Field } from '../../components/AuthForm'

type Setup = { secret: string; otpauth_uri: string }
type Mode = 'idle' | 'setup' | 'codes' | 'regenerate' | 'disable'

const PRIMARY =
  'rounded-md bg-indigo-500 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-indigo-400 disabled:opacity-50'
const SECONDARY =
  'rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-200 transition-colors hover:bg-zinc-800 disabled:opacity-50'
const DANGER =
  'rounded-md bg-rose-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-rose-500 disabled:opacity-50'

/** The authenticator secret in groups of four, which is how people copy it by hand. */
function groupSecret(secret: string) {
  return secret.replace(/(.{4})/g, '$1 ').trim()
}

/**
 * Settings → Two-factor authentication: turn it on, manage recovery codes, turn it off.
 *
 * Enrolling is three steps, and the order matters for safety (DESIGN.md §4.1):
 *  1. **Set up** — the server mints a *pending* secret; we show it as a QR code (plus the
 *     key itself for manual entry). Nothing is enforced yet.
 *  2. **Verify** — the user types a code from their app. Only a valid code turns 2FA on, so
 *     a botched scan can never lock someone out.
 *  3. **Save recovery codes** — shown exactly once (the server keeps only hashes). We keep
 *     them on screen until the user confirms they've saved them, and only then tell the
 *     parent the account changed (`onChanged`): refreshing the user earlier would flip this
 *     component to its "enabled" view and throw the codes away.
 *
 * Turning it off or regenerating recovery codes both need a current code (and, to turn it
 * off, the password), so a stolen session alone can't strip the second factor.
 *
 * @param enabled  whether the account has 2FA on, from the user object.
 * @param onChanged called after 2FA is enabled/disabled, to refresh the user.
 */
export function TwoFactor({
  enabled,
  onChanged,
}: {
  enabled: boolean
  onChanged: () => Promise<void> | void
}) {
  const [mode, setMode] = useState<Mode>('idle')
  const [setup, setSetup] = useState<Setup | null>(null)
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([])
  const [saved, setSaved] = useState(false)
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  function reset() {
    setMode('idle')
    setSetup(null)
    setRecoveryCodes([])
    setSaved(false)
    setCode('')
    setPassword('')
    setError('')
  }

  /** Run one request with shared busy/error handling; `then` runs only on success. */
  async function run(fn: () => Promise<void>) {
    setBusy(true)
    setError('')
    try {
      await fn()
    } catch (e) {
      setError(e instanceof HttpError ? e.message : 'Something went wrong.')
    } finally {
      setBusy(false)
    }
  }

  const start = () =>
    run(async () => {
      setSetup(await api.post<Setup>('/auth/2fa/setup'))
      setMode('setup')
    })

  const showCodes = (codes: string[]) => {
    setRecoveryCodes(codes)
    setSaved(false)
    setCode('')
    setMode('codes')
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    if (mode === 'setup')
      run(async () => showCodes((await api.post<{ recovery_codes: string[] }>('/auth/2fa/enable', { code: code.trim() })).recovery_codes))
    else if (mode === 'regenerate')
      run(async () => showCodes((await api.post<{ recovery_codes: string[] }>('/auth/2fa/recovery-codes', { code: code.trim() })).recovery_codes))
    else if (mode === 'disable')
      run(async () => {
        await api.post('/auth/2fa/disable', { password, code: code.trim() })
        reset()
        await onChanged()
      })
  }

  async function finish() {
    reset()
    await onChanged()
  }

  function download() {
    const blob = new Blob([`shikomi recovery codes\n\n${recoveryCodes.join('\n')}\n`], {
      type: 'text/plain',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'shikomi-recovery-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  const errorBox = error && (
    <div className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
      {error}
    </div>
  )

  // Recovery codes take precedence over everything: they exist only in this component's
  // state until acknowledged, whatever `enabled` says.
  if (mode === 'codes') {
    return (
      <div className="space-y-3">
        <p className="text-sm text-zinc-300">
          Save these recovery codes somewhere safe. Each works once if you lose your authenticator.
          <strong className="text-zinc-100"> We can't show them again.</strong>
        </p>
        <ul className="grid grid-cols-1 gap-1 rounded-md border border-zinc-800 bg-zinc-950/40 p-3 font-mono text-sm text-zinc-200 sm:grid-cols-2">
          {recoveryCodes.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-2">
          <button type="button" className={SECONDARY} onClick={() => navigator.clipboard?.writeText(recoveryCodes.join('\n'))}>
            Copy
          </button>
          <button type="button" className={SECONDARY} onClick={download}>
            Download
          </button>
        </div>
        <label className="flex items-center gap-2 text-sm text-zinc-400">
          <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} className="accent-indigo-500" />
          I've saved these codes somewhere safe
        </label>
        <button type="button" className={PRIMARY} disabled={!saved} onClick={finish}>
          Done
        </button>
      </div>
    )
  }

  if (mode === 'setup' && setup) {
    return (
      <form onSubmit={submit} className="space-y-3">
        <p className="text-sm text-zinc-300">
          Scan this QR code with an authenticator app (1Password, Authy, Google Authenticator…), then enter the 6-digit
          code it shows.
        </p>
        <div className="flex flex-wrap items-start gap-4">
          <div role="img" aria-label="QR code for your authenticator app" className="rounded-md bg-white p-1">
            <QRCodeSVG value={setup.otpauth_uri} size={160} marginSize={2} bgColor="#ffffff" fgColor="#000000" />
          </div>
          <div className="min-w-0 text-sm">
            <div className="text-zinc-500">Can't scan? Enter this key instead:</div>
            <code className="mt-1 block break-all font-mono text-zinc-200">{groupSecret(setup.secret)}</code>
          </div>
        </div>
        {errorBox}
        <Field label="6-digit code" value={code} onChange={setCode} autoComplete="one-time-code" autoFocus />
        <div className="flex gap-2">
          <button type="submit" className={PRIMARY} disabled={busy}>
            {busy ? 'Please wait…' : 'Verify and turn on'}
          </button>
          <button type="button" className={SECONDARY} onClick={reset} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    )
  }

  if (mode === 'regenerate' || mode === 'disable') {
    const turningOff = mode === 'disable'
    return (
      <form onSubmit={submit} className="space-y-3">
        <p className="text-sm text-zinc-300">
          {turningOff
            ? 'Turn off two-factor authentication. Enter your password and a current code (or a recovery code).'
            : 'Get a fresh set of recovery codes. The old ones stop working. Enter a current code to confirm.'}
        </p>
        {errorBox}
        {turningOff && <Field label="Password" type="password" value={password} onChange={setPassword} autoComplete="current-password" />}
        <Field label="Authentication code" value={code} onChange={setCode} autoComplete="one-time-code" autoFocus={!turningOff} />
        <div className="flex gap-2">
          <button type="submit" className={turningOff ? DANGER : PRIMARY} disabled={busy}>
            {busy ? 'Please wait…' : turningOff ? 'Turn off' : 'Generate new codes'}
          </button>
          <button type="button" className={SECONDARY} onClick={reset} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    )
  }

  return enabled ? (
    <div className="space-y-3">
      <p className="text-sm text-zinc-300">
        <span className="mr-2 inline-block rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-400 light:text-emerald-700">
          On
        </span>
        Signing in asks for a code from your authenticator app.
      </p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={SECONDARY} onClick={() => setMode('regenerate')}>
          New recovery codes
        </button>
        <button type="button" className={SECONDARY} onClick={() => setMode('disable')}>
          Turn off
        </button>
      </div>
    </div>
  ) : (
    <div className="space-y-3">
      <p className="text-sm text-zinc-400">
        Add a second step to signing in: a code from an authenticator app, on top of your password.
      </p>
      {errorBox}
      <button type="button" className={PRIMARY} onClick={start} disabled={busy}>
        {busy ? 'Please wait…' : 'Set up two-factor'}
      </button>
    </div>
  )
}
