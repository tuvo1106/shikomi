/** Small display helpers for the workspace. */
import type { Kind, ParamSpec } from '../../api/types'

/**
 * An operations-kind (design/class-replay) test case's input: `[ops, args]`,
 * `ops[0]` the class name (see judge/harness.py's "operations mode"). Callers must gate on `problem.kind
 * === 'operations'` before trusting this shape check — a function-kind
 * problem with two array-typed params (e.g. `merge(intervals, newInterval)`)
 * would also satisfy "two elements, both arrays", so shape alone isn't a safe
 * discriminator without the problem's own `kind` confirming which mode applies.
 */
export function isOperationsInput(input: unknown): input is [string[], unknown[][]] {
  return Array.isArray(input) && input.length === 2 &&
    Array.isArray(input[0]) && Array.isArray(input[1])
}

/**
 * Render a test case's input array as a readable arg list. For an operations-
 * kind case, `op(args); op(args); ...` (one call per op, so a design
 * problem's example reads as the sequence of calls it replays). Otherwise, with param metadata it's
 * `nums = [1,6], target = 7`; without it, the raw JSON array. This is why the
 * problem exposes `params` — so examples read like a function call, not a tuple.
 */
export function formatInput(input: unknown[], params: ParamSpec[], kind: Kind = 'function'): string {
  if (kind === 'operations' && isOperationsInput(input)) {
    const [ops, argLists] = input
    return ops
      .map((op, i) => `${op}(${(argLists[i] ?? []).map((a) => JSON.stringify(a)).join(', ')})`)
      .join('; ')
  }
  // A sql-kind case's input is `[seedScript]` (judge/harness_sql.py) — one
  // long semicolon-delimited string, not positional args. JSON.stringify-ing
  // it as an array (the params.length===0 fallback below) wraps it in `["…"]`
  // with every quote escaped, which reads as a wall of noise for something
  // that's really just a multi-statement SQL script. One statement per line
  // instead (paired with `whitespace-pre-wrap` where this is rendered).
  if (kind === 'sql') return formatSqlSeed(String(input[0] ?? ''))
  if (params.length === 0) return JSON.stringify(input)
  return params.map((p, i) => `${p.name} = ${JSON.stringify(input[i])}`).join(', ')
}

/** One SQL statement per line, trailing whitespace trimmed — see formatInput. */
export function formatSqlSeed(seedScript: string): string {
  return seedScript.trim().replace(/;\s*(?=\S)/g, ';\n')
}

/**
 * The leading `#`-comment block of `code` (e.g. a `ListNode`/`TreeNode`
 * definition — the convention for showing a class the judge already
 * provides; see judge/harness.py), or `''` if `code` doesn't start with one.
 */
function leadingComment(code: string): string {
  const lines = code.split('\n')
  let end = 0
  while (end < lines.length && lines[end].trimStart().startsWith('#')) {
    end++
  }
  return lines.slice(0, end).join('\n')
}

/**
 * Prefix `code` with `starterCode`'s leading comment block when `code` doesn't
 * already have one of its own — so loading a solution or a past submission
 * into the editor never drops the node-definition reference, even though
 * solutions/submissions are stored without it (the class comment is given
 * context, not something every snippet repeats).
 */
export function withReferenceComment(code: string, starterCode: string): string {
  if (code.trimStart().startsWith('#')) return code
  const prefix = leadingComment(starterCode)
  return prefix ? `${prefix}\n\n${code}` : code
}

/** Coarse "5m ago" relative time for submission timestamps (no i18n needed). */
export function relativeTime(iso: string): string {
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}
