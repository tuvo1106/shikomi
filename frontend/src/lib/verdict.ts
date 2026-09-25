/**
 * Client-side mirror of the backend's submission statuses. Kept in sync with
 * `models/submission.py::TERMINAL_STATUSES`: everything except `pending`/`running`
 * is a final verdict.
 */
const TERMINAL = new Set([
  'accepted',
  'wrong_answer',
  'runtime_error',
  'time_limit_exceeded',
  'memory_limit_exceeded',
  'output_limit_exceeded',
  'judge_error',
])

/**
 * Whether a status is final. The workspace polls a submission while this is false
 * and stops (showing the verdict) once it's true.
 */
export function isTerminal(status: string): boolean {
  return TERMINAL.has(status)
}

/** How often the workspace asks "done yet?" while a submission is judging. */
export const POLL_INTERVAL_MS = 1000

/**
 * How long the workspace keeps asking before it gives up. The server guarantees a terminal
 * status (`judge_error` at worst) about six minutes after a row goes quiet (a 5-minute
 * staleness threshold, checked once a minute: DESIGN.md §5.7), so this is just above that: a
 * healthy backend always answers first. Reaching it means the sweeper itself isn't running, and
 * polling forever would leave a spinner nobody can resolve.
 */
export const POLL_DEADLINE_MS = 7 * 60 * 1000

/**
 * The next `refetchInterval` for a judging submission: keep polling every second until the
 * status is terminal or `POLL_DEADLINE_MS` has passed since polling began, then stop (`false`).
 * A missing status (nothing fetched yet) also stops, as before: the first fetch is not a poll.
 */
export function nextPollDelay(
  status: string | undefined,
  startedAt: number,
  now: number,
): number | false {
  if (!status || isTerminal(status)) return false
  return now - startedAt >= POLL_DEADLINE_MS ? false : POLL_INTERVAL_MS
}

/** Human labels for each status code, for display in the UI. */
export const STATUS_LABEL: Record<string, string> = {
  pending: 'Pending',
  running: 'Running',
  accepted: 'Accepted',
  wrong_answer: 'Wrong Answer',
  runtime_error: 'Runtime Error',
  time_limit_exceeded: 'Time Limit Exceeded',
  memory_limit_exceeded: 'Memory Limit Exceeded',
  output_limit_exceeded: 'Output Limit Exceeded',
  judge_error: 'Judge Error',
}
