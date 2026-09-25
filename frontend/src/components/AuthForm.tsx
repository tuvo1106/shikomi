/**
 * Shared building blocks for the auth screens: a labeled `Field`, the centered
 * `AuthCard` form shell (title + error + submit + footer link), and `StatusCard`
 * for the non-form outcomes (verify/reset success). Reused across login, register,
 * forgot/reset password, and the verify gate for a consistent look.
 */
import type { FormEvent, ReactNode } from 'react'

/** A labeled text input used across the auth forms. */
export function Field({
  label,
  type = 'text',
  value,
  onChange,
  autoComplete,
  autoFocus,
}: {
  label: string
  type?: string
  value: string
  onChange: (v: string) => void
  autoComplete?: string
  autoFocus?: boolean
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm text-zinc-400">{label}</span>
      <input
        type={type}
        value={value}
        required
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none transition-colors focus:border-indigo-500"
      />
    </label>
  )
}

export function AuthCard({
  title,
  onSubmit,
  error,
  footer,
  busy,
  children,
}: {
  title: string
  onSubmit: (e: FormEvent) => void
  error?: string
  footer?: ReactNode
  busy?: boolean
  children: ReactNode
}) {
  return (
    <div className="flex min-h-[calc(100vh-3.5rem)] items-center justify-center p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-lg border border-zinc-800 bg-zinc-900/40 p-6"
      >
        <h1 className="text-lg font-semibold text-zinc-100">{title}</h1>
        {error && (
          <div className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
            {error}
          </div>
        )}
        {children}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-indigo-500 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-400 disabled:opacity-50"
        >
          {busy ? 'Please wait…' : title}
        </button>
        {footer && <p className="text-center text-sm text-zinc-500">{footer}</p>}
      </form>
    </div>
  )
}

/** Centered status card for non-form auth screens (verify / reset outcomes). */
export function StatusCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex min-h-[calc(100vh-3.5rem)] items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-3 rounded-lg border border-zinc-800 bg-zinc-900/40 p-6 text-center">
        <h1 className="text-lg font-semibold text-zinc-100">{title}</h1>
        <div className="text-sm text-zinc-400">{children}</div>
      </div>
    </div>
  )
}
