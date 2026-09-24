import { useEffect, useRef, type ReactNode } from 'react'

/**
 * The one modal shell: dark scrim + a centered panel that never outgrows the screen.
 *
 * Why it exists: three dialogs each hand-rolled the same scrim, and none capped
 * their height — a tall panel in a short viewport (a phone in landscape is ~375px
 * high) is centered by flexbox, so it overflows *both* ends and the top and the
 * buttons become unreachable. Here the panel is capped to the visible viewport
 * (`dvh`, so mobile browser chrome doesn't count) and scrolls internally instead.
 *
 * It also gives every dialog the basics they were missing: `role="dialog"` /
 * `aria-modal`, Escape to close, focus moved into the panel on open and restored on
 * close, and `overscroll-contain` so scrolling the panel doesn't scroll the page.
 *
 * `className` sets the panel's width/padding/alignment (e.g. `max-w-sm p-6`).
 */
export function Modal({
  label,
  onClose,
  className = '',
  children,
}: {
  /** Accessible name for the dialog (there is often no visible title to point at). */
  label: string
  onClose: () => void
  className?: string
  children: ReactNode
}) {
  const panel = useRef<HTMLDivElement>(null)
  // Callers pass a fresh inline `onClose` every render. Reading it through a ref keeps
  // the mount effect below from re-running (which would hand focus back to the opener
  // mid-life and re-bind the key listener on every parent render).
  const onCloseRef = useRef(onClose)
  useEffect(() => {
    onCloseRef.current = onClose
  })

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    panel.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      previous?.focus?.()
    }
  }, [])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        className={`max-h-[calc(100dvh-2rem)] w-full overflow-y-auto overscroll-contain rounded-lg border border-zinc-800 bg-zinc-900 outline-none ${className}`}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}
