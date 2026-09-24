/** The bottom-right results pane: judging spinner, error, or the verdict + cases. */
import { useEffect, useState, type ReactNode } from 'react'
import type { CaseResult, Kind, ParamSpec, ProblemDetail, SampleCase, Submission } from '../../api/types'
import { STATUS_LABEL } from '../../lib/verdict'
import { formatSqlSeed, isOperationsInput } from './format'
import { SqlRowsTable } from './SqlRowsTable'
import { CodeBlock } from './CodeBlock'

/**
 * Renders the current results state: an inline error, a judging placeholder, an
 * empty prompt, or the `Verdict` (status, X/N passed, and a per-case tab strip
 * defaulting to the first failing case).
 */
export function ResultsBody({
  judging,
  submission,
  error,
  problem,
}: {
  judging: boolean
  submission?: Submission
  error: string
  problem: ProblemDetail
}) {
  if (error) return <div className="text-rose-400 light:text-rose-600">{error}</div>
  if (judging)
    return (
      <div className="space-y-2">
        <div className="text-zinc-400">Judging…</div>
        <div className="h-4 w-40 animate-pulse rounded bg-zinc-900" />
        <div className="h-4 w-24 animate-pulse rounded bg-zinc-900" />
      </div>
    )
  if (!submission)
    return <div className="text-zinc-600">Run against sample cases, or Submit to judge all cases.</div>
  return <Verdict submission={submission} problem={problem} />
}

function Verdict({ submission, problem }: { submission: Submission; problem: ProblemDetail }) {
  const results = submission.verdict_detail?.results ?? []
  const [active, setActive] = useState(0)

  // Default to the first failing case when a new verdict arrives.
  useEffect(() => {
    const firstFail = results.findIndex((r) => r.status !== 'passed')
    setActive(firstFail >= 0 ? firstFail : 0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submission.id])

  const accepted = submission.status === 'accepted'
  const vd = submission.verdict_detail
  const sampleByOrdinal = new Map(problem.sample_cases.map((s) => [s.ordinal, s]))
  const current = results[active]

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <span
          className={
            accepted
              ? 'font-semibold text-emerald-400 light:text-emerald-600'
              : 'font-semibold text-rose-400 light:text-rose-600'
          }
        >
          {STATUS_LABEL[submission.status] ?? submission.status}
        </span>
        {vd && (
          <span className="text-zinc-500">
            {vd.passed}/{vd.total} passed
          </span>
        )}
      </div>

      {results.length > 0 && (
        <>
          <div className="flex flex-wrap gap-1.5">
            {results.map((r, i) => (
              <button
                key={r.test_case_id}
                onClick={() => setActive(i)}
                className={`flex items-center gap-1.5 rounded px-2 py-1 text-xs ${
                  i === active ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:bg-zinc-800/50'
                }`}
              >
                <span
                  className={
                    r.status === 'passed'
                      ? 'text-emerald-400 light:text-emerald-600'
                      : 'text-rose-400 light:text-rose-600'
                  }
                >
                  ●
                </span>
                Case {i + 1}
              </button>
            ))}
          </div>
          {current && (
            <CaseDetail
              result={current}
              sample={sampleByOrdinal.get(current.test_case_id)}
              params={problem.params}
              kind={problem.kind}
            />
          )}
        </>
      )}
    </div>
  )
}

/** A sql-kind case's `result.output` is the harness's own JSON-stringified row
 * list (judge/harness_sql.py's `json.dumps(rows, ...)`), not the raw array
 * `Expected` gets — parse it back to render as a table, falling back to the
 * plain string on anything unparseable rather than throwing. */
function SqlOutputRows({ output }: { output: string }) {
  try {
    const rows = JSON.parse(output)
    if (Array.isArray(rows)) return <SqlRowsTable rows={rows} />
  } catch {
    // fall through to the raw string below
  }
  return <>{output}</>
}

function ResultField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-0.5 text-xs font-medium text-zinc-500">{label}</div>
      <div className="overflow-auto rounded bg-zinc-900/60 px-2 py-1 font-mono text-xs text-zinc-300">
        {children}
      </div>
    </div>
  )
}

function CaseDetail({
  result,
  sample,
  params,
  kind,
}: {
  result: CaseResult
  sample?: SampleCase
  params: ParamSpec[]
  kind: Kind
}) {
  // Samples carry input/expected from the problem; a revealed failing hidden case
  // carries them embedded on the result itself, wrapped as {value, truncated}.
  const inputVals = sample?.input ?? result.input?.value
  const expectedVal = sample ? sample.expected : result.expected?.value
  // Gate the shape check on `kind` (not shape alone) — see isOperationsInput's
  // docstring for why a function-kind case could otherwise false-positive.
  const isOperations = kind === 'operations' && isOperationsInput(inputVals)

  return (
    <div className="space-y-2">
      {inputVals != null ? (
        isOperations ? (
          <OperationsTrace ops={inputVals[0]} argLists={inputVals[1]}
                           expected={Array.isArray(expectedVal) ? expectedVal : []} />
        ) : kind === 'sql' && Array.isArray(inputVals) ? (
          // Broken out of ResultField (below) rather than nested in it — CodeBlock
          // brings its own background/padding, so wrapping it in ResultField's box
          // too would double up. Same label style, applied by hand.
          <div>
            <div className="mb-0.5 text-xs font-medium text-zinc-500">Input</div>
            <CodeBlock code={formatSqlSeed(String(inputVals[0] ?? ''))} language="mysql" copyable={false} />
          </div>
        ) : (
          <ResultField label="Input">
            {Array.isArray(inputVals) && params.length > 0 ? (
              params.map((p, i) => (
                <div key={p.name}>
                  <span className="text-zinc-500">{p.name} = </span>
                  {JSON.stringify(inputVals[i])}
                </div>
              ))
            ) : (
              <div>{typeof inputVals === 'string' ? inputVals : JSON.stringify(inputVals)}</div>
            )}
          </ResultField>
        )
      ) : (
        <div className="text-xs text-zinc-600">Hidden test case — input and expected are not shown.</div>
      )}
      {result.output != null && (
        <ResultField label="Output">
          {kind === 'sql' ? <SqlOutputRows output={result.output} /> : result.output}
        </ResultField>
      )}
      {inputVals != null && !isOperations && (
        <ResultField label="Expected">
          {kind === 'sql' && Array.isArray(expectedVal) ? (
            <SqlRowsTable rows={expectedVal as unknown[][]} />
          ) : typeof expectedVal === 'string' ? (
            expectedVal
          ) : (
            JSON.stringify(expectedVal)
          )}
        </ResultField>
      )}
      {result.error && (
        <ResultField label="Error">
          <pre className="whitespace-pre-wrap text-rose-300">{result.error}</pre>
        </ResultField>
      )}
      {result.stdout && (
        <ResultField label="Stdout">
          <pre className="whitespace-pre-wrap text-zinc-400">{result.stdout}</pre>
        </ResultField>
      )}
    </div>
  )
}

/** An operations-kind (design/class-replay) case's op sequence, one call per
 * line — `op(args) → result`, skipping the arrow on the constructor (index 0,
 * whose result is always null per the wire convention). Replaces the
 * params-positional rendering above, which has nothing to map an ops/args
 * pair against. */
function OperationsTrace({
  ops,
  argLists,
  expected,
}: {
  ops: string[]
  argLists: unknown[][]
  expected: unknown[]
}) {
  return (
    <ResultField label="Operations">
      <div className="space-y-0.5">
        {ops.map((op, i) => (
          <div key={i}>
            <span className="text-zinc-500">{op}</span>
            {`(${(argLists[i] ?? []).map((a) => JSON.stringify(a)).join(', ')})`}
            {i > 0 && (
              <span className="text-zinc-500"> {'→'} {JSON.stringify(expected[i] ?? null)}</span>
            )}
          </div>
        ))}
      </div>
    </ResultField>
  )
}
