/** Shared presentational primitives for the workspace panels (styling only). */
import type { ReactNode } from 'react'
import { PanelResizeHandle } from 'react-resizable-panels'

/** The shared card chrome (border/rounding/fill) every panel section wears. */
export const CARD =
  'flex h-full flex-col overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900/30'

/** Vertical drag handle for a horizontal panel group (width-styled). */
export function HandleX() {
  return (
    <PanelResizeHandle className="group flex w-3 items-center justify-center">
      <div className="h-8 w-1 rounded-full bg-zinc-800 transition-colors group-hover:bg-indigo-500" />
    </PanelResizeHandle>
  )
}

export function HandleY() {
  return (
    <PanelResizeHandle className="group flex h-3 items-center justify-center">
      <div className="h-1 w-8 rounded-full bg-zinc-800 transition-colors group-hover:bg-indigo-500" />
    </PanelResizeHandle>
  )
}

export function LeftTab({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`border-b-2 py-2 transition-colors ${
        active ? 'border-indigo-500 text-zinc-100' : 'border-transparent text-zinc-400 hover:text-zinc-200'
      }`}
    >
      {children}
    </button>
  )
}

export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">{children}</div>
  )
}
