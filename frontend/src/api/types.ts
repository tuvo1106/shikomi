/**
 * TypeScript mirrors of the backend Pydantic schemas (`app/schemas/`). These are
 * the wire contract: keep field names/shapes in sync with the API. Split by area.
 */

/** The authenticated user (mirror of `UserOut`) — no credentials, ever. */
export type User = {
  id: string
  email: string
  username: string
  email_verified: boolean
  /** Two-factor auth is on: Settings offers manage/turn-off instead of set-up. */
  totp_enabled: boolean
}

export type Difficulty = 'easy' | 'medium' | 'hard'
export type UserStatus = 'solved' | 'attempted' | 'unsolved'
/** "function" (default): a single top-level function, called once per test
 * case. "operations": a design/class-replay problem (a cache, a state
 * machine) — the judge instantiates `class_name` once per test case and replays
 * a sequence of method calls against it. "sql": a query against a seeded
 * schema (judge/harness_sql.py) — no function/class to call. */
export type Kind = 'function' | 'operations' | 'sql'
/** Which harness/sandbox judges the submission (DESIGN.md §13). "js" only
 * supports `kind: "function"` — no operations mode, no ListNode/TreeNode.
 * "mysql" always pairs with `kind: "sql"`, never any other kind. */
export type Language = 'python' | 'js' | 'mysql'

export type ProblemListItem = {
  id: string
  slug: string
  title: string
  difficulty: Difficulty
  tags: string[]
  user_status: UserStatus
}

/** Full filter vocabulary for the catalog (`GET /problems/facets`) — distinct
 * across every published problem, so the dropdowns aren't limited to whichever
 * page is currently loaded. */
export type ProblemFacets = {
  tags: string[]
  collections: string[]
}

export type ProblemListResponse = {
  items: ProblemListItem[]
  total: number
}

export type ParamSpec = { name: string; type: string }
export type SampleCase = { ordinal: number; input: unknown[]; expected: unknown }

export type ProblemDetail = {
  id: string
  slug: string
  title: string
  difficulty: Difficulty
  statement_md: string
  starter_code: string
  kind: Kind
  language: Language
  function_name: string | null
  class_name: string | null
  params: ParamSpec[]
  return_type: string
  tags: string[]
  constraints: string[]
  sample_cases: SampleCase[]
  has_solutions: boolean
  user_status: UserStatus
}

export type SubmissionStatus =
  | 'pending'
  | 'running'
  | 'accepted'
  | 'wrong_answer'
  | 'runtime_error'
  | 'time_limit_exceeded'
  | 'memory_limit_exceeded'
  | 'output_limit_exceeded'
  | 'judge_error'

export type CaseResult = {
  test_case_id: number
  status: string
  runtime_ms: number | null
  output?: string | null
  stdout?: string | null
  error?: string | null
  // Present only on the revealed first-failing hidden case (samples carry their
  // own). `value` is a truncated JSON-string preview (not the original shape)
  // when `truncated` is true, so a large case doesn't bloat the response.
  input?: { value: unknown; truncated: boolean }
  expected?: { value: unknown; truncated: boolean }
}

export type VerdictDetail = { results: CaseResult[]; passed: number; total: number }

export type Solution = {
  id: string
  ordinal: number
  title: string
  intuition_md: string
  algorithm_md: string
  code: string
  time_complexity: string
  space_complexity: string
  time_complexity_reason: string
  space_complexity_reason: string
}

export type SolutionsResponse = { items: Solution[] }

export type Submission = {
  id: string
  problem_id: string
  status: SubmissionStatus
  code: string
  verdict_detail: VerdictDetail | null
  runtime_ms: number | null
  is_run: boolean
  created_at: string
  runtime_percentile?: number | null
}

export type SubmissionListItem = {
  id: string
  status: string
  runtime_ms: number | null
  created_at: string
}

export type SubmissionListResponse = { items: SubmissionListItem[] }

export type RuntimeDistribution = {
  buckets: number[]
  lo: number
  hi: number
  total: number
}
