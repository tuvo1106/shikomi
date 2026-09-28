/**
 * Per-language presentation: Monaco's language id, the human label, and the
 * "beats X%" wording. One place to extend when a judge gains a language
 * (DESIGN.md §13); `Record<Language, …>` makes a missing entry a type error.
 */
import type { Language } from '../api/types'

/** Monaco's built-in language id for each judge language. */
export const MONACO_LANGUAGE: Record<Language, string> = {
  python: 'python',
  js: 'javascript',
  rust: 'rust',
  mysql: 'sql',
}

/** How a language is named in the UI (switcher, chips, "beats X% of … submissions"). */
export const LANGUAGE_LABEL: Record<Language, string> = {
  python: 'Python3',
  js: 'JavaScript',
  rust: 'Rust',
  mysql: 'MySQL',
}

/**
 * The runtime-percentile line for an accepted submission.
 *
 * The server compares only against the same language (a compiled Rust run
 * would beat every Python one otherwise), so on a multi-language problem the
 * sentence names the language; on a one-language problem that's noise.
 */
export function beatsText(percentile: number | null | undefined, language: Language, multiLanguage: boolean): string {
  const kind = multiLanguage ? `accepted ${LANGUAGE_LABEL[language]} submission` : 'accepted submission'
  return percentile != null ? `Beats ${percentile}% of ${kind}s` : `You're the first ${kind}!`
}
