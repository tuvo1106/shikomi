import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Editor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import remarkMath from 'remark-math'
import remarkGfm from 'remark-gfm'
import rehypeKatex from 'rehype-katex'
import { Panel, PanelGroup } from 'react-resizable-panels'
import { RotateCcw } from 'lucide-react'
import { useTheme } from '../../lib/theme'
import { useMediaQuery } from '../../lib/useMediaQuery'
import { api, HttpError } from '../../api/client'
import type { Language, ProblemDetail, Submission } from '../../api/types'
import { DifficultyBadge } from '../../components/badges'
import { isTerminal, nextPollDelay, POLL_DEADLINE_MS } from '../../lib/verdict'
import { CARD, HandleX, HandleY, LeftTab } from './ui'
import { formatInput, formatSqlSeed, withReferenceComment } from './format'
import { CodeBlock } from './CodeBlock'
import { SampleDiagrams } from './SampleDiagrams'
import { Solutions } from './Solutions'
import { Submissions } from './Submissions'
import { SuccessModal } from './SuccessModal'
import { ResultsBody } from './Results'

// Monaco's built-in language id and the header's human label, per problem
// `language` (DESIGN.md §13) — one place to add a fourth language later.
const MONACO_LANGUAGE: Record<Language, string> = { python: 'python', js: 'javascript', mysql: 'sql' }
const LANGUAGE_LABEL: Record<Language, string> = { python: 'Python3', js: 'JavaScript', mysql: 'MySQL' }

/**
 * The problem-solving screen: description/solutions/submissions on the left, the
 * Monaco editor + results on the right (stacked on mobile). It owns the editor
 * state and the Run/Submit → poll → verdict loop; the panels are presentational.
 *
 * Two behaviors worth knowing:
 * - **Draft persistence**: the editor's code is mirrored to `localStorage` per
 *   slug, so a refresh or navigating away doesn't lose work (and E2E seeds it).
 * - **Verdict polling**: submitting returns an id; the query below re-fetches on
 *   an interval until the status is terminal (see `refetchInterval`), which is how
 *   an async verdict shows up without websockets.
 */
export default function Workspace() {
  const { slug } = useParams<{ slug: string }>()
  // Keyed by slug so moving between problems (the "Next problem" button, browser
  // back/forward) remounts the screen. React Router reuses one element across
  // `/problems/:slug` routes, so without this the active tab, the last verdict and
  // its polling id, the error text and the modals all leaked into the next problem.
  // A remount resets every piece of state at once, including any added later.
  return <ProblemWorkspace key={slug} />
}

function ProblemWorkspace() {
  const { slug } = useParams<{ slug: string }>()
  const { theme } = useTheme()
  // Below md, stack the panels vertically (description → editor → results)
  // instead of the side-by-side split, which is unusable on a phone.
  const isDesktop = useMediaQuery('(min-width: 768px)')
  const { data: problem, isLoading, isError } = useQuery({
    queryKey: ['problem', slug],
    queryFn: () => api.get<ProblemDetail>(`/problems/${slug}`),
  })

  const [code, setCode] = useState('')
  const [submissionId, setSubmissionId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [pollTimedOut, setPollTimedOut] = useState(false)
  const pollStartedAt = useRef(0)
  const [actionError, setActionError] = useState('')
  const [leftTab, setLeftTab] = useState<'description' | 'solutions' | 'submissions'>('description')
  const [modalSub, setModalSub] = useState<Submission | null>(null)
  const [showSchema, setShowSchema] = useState(false)
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!problem) return
    setCode(localStorage.getItem(`code:${problem.slug}`) ?? problem.starter_code)
  }, [problem])

  function onCodeChange(value: string | undefined) {
    const next = value ?? ''
    setCode(next)
    if (problem) localStorage.setItem(`code:${problem.slug}`, next)
  }

  // Poll the active submission every second until its status is terminal, then
  // stop (returning false from refetchInterval halts polling). `enabled` keeps it
  // idle until a Run/Submit sets an id.
  const { data: submission } = useQuery({
    queryKey: ['submission', submissionId],
    queryFn: () => api.get<Submission>(`/submissions/${submissionId}`),
    enabled: !!submissionId,
    refetchInterval: (query) =>
      nextPollDelay(query.state.data?.status, pollStartedAt.current, Date.now()),
  })

  // Polling stops at POLL_DEADLINE_MS (see `nextPollDelay`); this timer is what tells the UI, since
  // a stopped query doesn't re-render. Without it a submission the server never settles would
  // leave "Judging…" spinning forever.
  useEffect(() => {
    setPollTimedOut(false)
    if (!submissionId) return
    const timer = setTimeout(() => setPollTimedOut(true), POLL_DEADLINE_MS)
    return () => clearTimeout(timer)
  }, [submissionId])

  const stillJudging = !!submission && !isTerminal(submission.status)
  const timedOut = pollTimedOut && stillJudging
  const judging = starting || (stillJudging && !timedOut)
  const proseInvert = theme === 'dark' ? 'prose-invert' : ''

  // Celebrate a fresh accepted Submit, and refresh derived views (history, list badge).
  useEffect(() => {
    if (submission?.status === 'accepted' && !submission.is_run) {
      setModalSub(submission)
      queryClient.invalidateQueries({ queryKey: ['submissions', problem?.slug] })
      queryClient.invalidateQueries({ queryKey: ['problem', problem?.slug] })
      queryClient.invalidateQueries({ queryKey: ['problems'] })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submission?.id, submission?.status])

  async function run(path: '/submissions' | '/run') {
    if (!problem) return
    setActionError('')
    setStarting(true)
    setSubmissionId(null)
    pollStartedAt.current = Date.now()
    try {
      const res = await api.post<{ id: string }>(path, { problem_id: problem.id, code })
      setSubmissionId(res.id)
    } catch (e) {
      setActionError(e instanceof HttpError ? e.message : 'Failed to submit.')
    } finally {
      setStarting(false)
    }
  }

  function resetCode() {
    if (!problem) return
    setCode(problem.starter_code)
    localStorage.removeItem(`code:${problem.slug}`)
  }

  function loadCode(c: string) {
    if (!problem) return
    const next = withReferenceComment(c, problem.starter_code)
    setCode(next)
    localStorage.setItem(`code:${problem.slug}`, next)
  }

  if (isLoading) return <div className="p-6 text-sm text-zinc-500">Loading…</div>
  if (isError || !problem) return <div className="p-6 text-sm text-rose-400 light:text-rose-600">Problem not found.</div>

  return (
    <div className="h-[calc(100vh-3.5rem)] p-2 sm:p-3">
      <PanelGroup key={isDesktop ? 'h' : 'v'} direction={isDesktop ? 'horizontal' : 'vertical'}>
        <Panel defaultSize={45} minSize={20}>
          <section className={CARD}>
            <header className="flex items-center gap-3 px-4 py-2.5">
              <h1 className="text-base font-semibold text-zinc-100">{problem.title}</h1>
              <DifficultyBadge value={problem.difficulty} />
              {problem.user_status === 'solved' && (
                <span className="ml-auto rounded bg-emerald-500/10 px-1.5 py-0.5 text-xs font-medium text-emerald-400 light:bg-emerald-500/15 light:text-emerald-700">
                  ✓ Solved
                </span>
              )}
            </header>
            <div className="flex gap-4 border-b border-zinc-800 px-4 text-sm">
              <LeftTab active={leftTab === 'description'} onClick={() => setLeftTab('description')}>
                Description
              </LeftTab>
              {problem.has_solutions && (
                <LeftTab active={leftTab === 'solutions'} onClick={() => setLeftTab('solutions')}>
                  Solutions
                </LeftTab>
              )}
              <LeftTab active={leftTab === 'submissions'} onClick={() => setLeftTab('submissions')}>
                Submissions
              </LeftTab>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-4">
              {leftTab === 'description' ? (
                <div className="space-y-4">
                  {/* Statements carry inline and display math ($O(\log n)$, the
                      Tribonacci recurrence) on the same footing as Constraints
                      below, so they get the same remarkMath/rehypeKatex pair.
                      remarkMath skips $ inside code spans, which is what keeps
                      prose like `"$"` (a trie sentinel) rendering literally. */}
                  <article className={`prose prose-sm max-w-none ${proseInvert}`}>
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm, remarkMath]}
                      rehypePlugins={[rehypeKatex]}
                    >
                      {problem.statement_md}
                    </ReactMarkdown>
                  </article>
                  {problem.kind === 'sql' ? (
                    // The statement above already shows a worked example (schema +
                    // input/result tables, the usual shape of a SQL problem
                    // statement) — this toggle isn't a second example, it's the actual
                    // seed SQL the harness runs, for anyone who wants to see exactly
                    // what gets executed rather than the hand-written prose version.
                    // Only Input: the Output would just repeat the statement's own
                    // Result table.
                    <div>
                      <button
                        onClick={() => setShowSchema((v) => !v)}
                        className="text-sm font-medium text-indigo-400 hover:text-indigo-300"
                      >
                        {showSchema ? 'Hide schema' : 'Show schema'}
                      </button>
                      {showSchema && (
                        <div className="mt-2 space-y-4">
                          {problem.sample_cases.map((sc, i) => (
                            <div key={sc.ordinal}>
                              {problem.sample_cases.length > 1 && (
                                <div className="mb-1 text-sm font-semibold text-zinc-200">Example {i + 1}</div>
                              )}
                              <div className="text-xs">
                                <CodeBlock
                                  code={formatSqlSeed(String(sc.input[0] ?? ''))}
                                  language="mysql"
                                  copyable={false}
                                />
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ) : (
                    problem.sample_cases.map((sc, i) => (
                      <div key={sc.ordinal}>
                        <div className="mb-1 text-sm font-semibold text-zinc-200">Example {i + 1}</div>
                        <div className="space-y-1 rounded border border-zinc-800 bg-zinc-900/40 p-2 font-mono text-xs">
                          <div>
                            <span className="text-zinc-500">Input: </span>
                            <span className="text-zinc-300">{formatInput(sc.input, problem.params, problem.kind)}</span>
                          </div>
                          <div>
                            <span className="text-zinc-500">Output: </span>
                            <span className="text-zinc-300">{JSON.stringify(sc.expected)}</span>
                          </div>
                          {/* A TreeNode/ListNode/GraphNode case's arrays above are
                              the judge's wire format, not something you can read a
                              shape out of — the diagram is the shape. */}
                          <SampleDiagrams problem={problem} sample={sc} />
                        </div>
                      </div>
                    ))
                  )}
                  {problem.constraints.length > 0 && (
                    <div>
                      <h3 className="mb-1 text-sm font-semibold text-zinc-200">Constraints</h3>
                      <ul className={`constraints prose prose-sm max-w-none list-disc pl-5 ${proseInvert}`}>
                        {problem.constraints.map((c, i) => (
                          <li key={i}>
                            <ReactMarkdown
                              remarkPlugins={[remarkMath]}
                              rehypePlugins={[rehypeKatex]}
                              components={{ p: ({ children }) => <>{children}</> }}
                            >
                              {c}
                            </ReactMarkdown>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ) : leftTab === 'solutions' ? (
                <Solutions
                  slug={problem.slug}
                  solved={problem.user_status === 'solved'}
                  language={problem.language}
                  onLoadCode={loadCode}
                />
              ) : (
                <Submissions slug={problem.slug} language={problem.language} onLoadCode={loadCode} />
              )}
            </div>
          </section>
        </Panel>

        {isDesktop ? <HandleX /> : <HandleY />}

        <Panel defaultSize={55} minSize={30}>
          <PanelGroup direction="vertical">
            <Panel defaultSize={65} minSize={20}>
              {/* `isolate` gives the editor its own stacking context: `.monaco-editor`
                  doesn't create one, so without this every Monaco z-index (suggest
                  widget 40, rename box 100, overlay message 10000) competes in the root
                  stacking context and can paint over the navbar's dropdown (`Navbar.tsx`,
                  z-40). Don't move this up to `<main>` — that would trap `Modal`'s z-50
                  below the bar too. */}
              <section className={`${CARD} isolate`}>
                <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-1.5 text-xs">
                  <span className="text-zinc-500">{LANGUAGE_LABEL[problem.language]}</span>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={resetCode}
                      title="Reset to starter code"
                      className="mr-1 text-zinc-500 hover:text-zinc-300"
                    >
                      <RotateCcw size={14} strokeWidth={1.5} />
                    </button>
                    <button
                      onClick={() => run('/run')}
                      disabled={judging}
                      className="rounded border border-zinc-700 px-3 py-1 text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                    >
                      Run
                    </button>
                    <button
                      onClick={() => run('/submissions')}
                      disabled={judging}
                      className="w-[84px] rounded bg-indigo-500 px-3 py-1 text-center font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
                    >
                      {judging ? 'Judging…' : 'Submit'}
                    </button>
                  </div>
                </header>
                <div className="min-h-0 flex-1 py-2">
                  <Editor
                    height="100%"
                    language={MONACO_LANGUAGE[problem.language]}
                    theme={theme === 'dark' ? 'vs-dark' : 'light'}
                    value={code}
                    onChange={onCodeChange}
                    options={{
                      minimap: { enabled: false },
                      fontSize: 14,
                      scrollBeyondLastLine: false,
                      fontLigatures: true,
                      padding: { top: 12, bottom: 12 },
                      lineNumbersMinChars: 3,
                    }}
                  />
                </div>
              </section>
            </Panel>

            <HandleY />

            <Panel defaultSize={35} minSize={15}>
              <section className={CARD}>
                <header className="border-b border-zinc-800 px-4 py-2 text-xs font-medium text-zinc-400">
                  Results
                </header>
                <div className="min-h-0 flex-1 overflow-auto p-4 text-sm">
                  <ResultsBody
                    judging={judging}
                    submission={submission}
                    error={
                      actionError ||
                      (timedOut
                        ? 'Judging is taking longer than expected. It may still finish: check the Submissions tab, or try again.'
                        : '')
                    }
                    problem={problem}
                  />
                </div>
              </section>
            </Panel>
          </PanelGroup>
        </Panel>
      </PanelGroup>
      {modalSub && (
        <SuccessModal
          submission={modalSub}
          problemSlug={problem.slug}
          onClose={() => setModalSub(null)}
          onViewSubmissions={() => {
            setLeftTab('submissions')
            setModalSub(null)
          }}
        />
      )}
    </div>
  )
}
