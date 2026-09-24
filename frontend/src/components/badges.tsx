/**
 * Small status pills. The `light:` classes are explicit because these accent
 * colors don't flip with the inverted-zinc theme trick — they need darker shades
 * chosen by hand for light mode (see index.css).
 */
import type { Difficulty, UserStatus } from '../api/types'

const DIFFICULTY_STYLES: Record<Difficulty, string> = {
  easy: 'text-teal-300 bg-teal-500/10 light:text-teal-700 light:bg-teal-500/15',
  medium: 'text-amber-300 bg-amber-500/10 light:text-amber-700 light:bg-amber-500/15',
  hard: 'text-rose-300 bg-rose-500/10 light:text-rose-700 light:bg-rose-500/15',
}

/** A colored difficulty pill (easy/medium/hard). */
export function DifficultyBadge({ value }: { value: Difficulty }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium capitalize ${DIFFICULTY_STYLES[value]}`}>
      {value}
    </span>
  )
}

export function StatusBadge({ value }: { value: UserStatus }) {
  if (value === 'solved')
    return <span title="Solved" className="text-emerald-400 light:text-emerald-600">✓</span>
  if (value === 'attempted')
    return <span title="Attempted" className="text-amber-400 light:text-amber-600">◐</span>
  return <span title="Unsolved" className="text-zinc-700">·</span>
}
