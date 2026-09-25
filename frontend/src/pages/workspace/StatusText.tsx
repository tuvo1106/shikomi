import { STATUS_LABEL } from '../../lib/verdict'

/** Verdict status in the accepted-green / rejected-rose color, light-mode aware. */
export function StatusText({ status }: { status: string }) {
  const accepted = status === 'accepted'
  return (
    <span
      className={
        accepted ? 'text-emerald-400 light:text-emerald-600' : 'text-rose-400 light:text-rose-600'
      }
    >
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}
