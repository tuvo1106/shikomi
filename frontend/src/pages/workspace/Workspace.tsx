import { useEffect, useMemo, useRef, useState } from 'react'
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
import type { Language, LanguageVariant, ProblemDetail, Submission } from '../../api/types'
import { LANGUAGE_LABEL, MONACO_LANGUAGE } from '../../lib/languages'
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
import { LanguageSwitcher } from './LanguageSwitcher'

/** localStorage key of a problem's draft in one language. */
const draftKey = (slug: string, language: Language) => `code:${slug}:${language}`
/** localStorage key remembering which language a problem was last open in. */
const languageKey = (slug: string) => `lang:${slug}`

/** The saved draft for `language`, or null. A pure read: safe during render. */
function readDraft(problem: ProblemDetail, language: Language): string | null {
  return localStorage.getItem(draftKey(problem.slug, language))
}

/**
 * Moves a draft saved before problems had several languages (under the bare
 * `code:<slug>` key) to its per-language key. That's only safe when the problem
 * has one language: a problem's default language can change when it gains more
 * (`merge-booking-windows` went from Rust to Python), so on a multi-language
 * problem nothing says which language an old draft was written in, and filing it
 * under the wrong one would submit Rust as Python. There it's left untouched.
 */
function migrateLegacyDraft(problem: ProblemDetail) {
  const legacyKey = `code:${problem.slug}`
  const legacy = localStorage.getItem(legacyKey)
  if (legacy === null || problem.languages.length !== 1) return
  const key = draftKey(problem.slug, problem.languages[0].language)
  if (localStorage.getItem(key) === null) localStorage.setItem(key, legacy)
  localStorage.removeItem(legacyKey)
}

/** The language to open in: the one last used on this problem if it still
 * offers it, else the problem's default (its first language). */
function initialLanguage(problem: ProblemDetail): Language {
  const saved = localStorage.getItem(languageKey(problem.slug))
  return problem.languages.find((v) => v.language === saved)?.language ?? problem.languages[0].language
}

/**
 * The problem-solving screen: description/solutions/submissions on the left, the
 * Monaco editor + results on the right (stacked on mobile). It owns the editor
 * state and the Run/Submit → poll → verdict loop; the panels are presentational.
 *
 * Two behaviors worth knowing:
 * - **Draft persistence**: the editor's code is mirrored to `localStorage` per
 *   slug *and language*, so a refresh, navigating away, or switching languages
 *   never loses work (and E2E seeds it).
 * - **Languages**: a problem offers one or more languages
 *   (docs/adr/0005-multi-language-problems.md). With several, the editor header
 *   gets a switcher; each language keeps its own draft, and Run/Submit send the
 *   selected one.
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
  const [language, setLanguage] = useState<Language | null>(null)
  const [submissionId, setSubmissionId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [pollTimedOut, setPollTimedOut] = useState(false)
  const pollStartedAt = useRef(0)
  const [actionError, setActionError] = useState('')
  const [leftTab, setLeftTab] = useState<'description' | 'solutions' | 'submissions'>('description')
  const [modalSub, setModalSub] = useState<Submission | null>(null)
  const [showSchema, setShowSchema] = useState(false)
  const queryClient = useQueryClient()

  // The selected language's variant. Falls back to the default so a render
  // before the effect below has picked a language still has one to show.
  const variant: LanguageVariant | undefined =
    problem?.languages.find((v) => v.language === language) ?? problem?.languages[0]
  const multiLanguage = (problem?.languages.length ?? 0) > 1

  useEffect(() => {
    if (!problem) return
    migrateLegacyDraft(problem)
    const lang = initialLanguage(problem)
    const v = problem.languages.find((l) => l.language === lang)!
    setLanguage(lang)
    setCode(readDraft(problem, lang) ?? v.starter_code)
  }, [problem])

  function onCodeChange(value: string | undefined) {
    const next = value ?? ''
    setCode(next)
    if (problem && variant) localStorage.setItem(draftKey(problem.slug, variant.language), next)
  }

  /** Switch the editor to `next`, bringing up that language's own draft. The
   * current draft is already saved (every edit is), so nothing is lost. Refused
   * while a verdict is pending: the result belongs to the language it was
   * submitted in (every caller also disables its control; this is the backstop). */
  function switchLanguage(next: Language) {
    if (!problem || next === variant?.language || judging) return
    const v = problem.languages.find((l) => l.language === next)
    if (!v) return
    setLanguage(next)
    setCode(readDraft(problem, next) ?? v.starter_code)
    localStorage.setItem(languageKey(problem.slug), next)
  }

  /** Which languages hold a draft that differs from their starter code, for the
   * switcher's "edited" dots. The other languages' drafts are read from storage
   * once per switch (they can't change while this one is being edited); only the
   * current language is compared per keystroke. */
  const editedElsewhere = useMemo(() => {
    if (!problem) return [] as Language[]
    return problem.languages
      .filter((v) => v.language !== variant?.language)
      .filter((v) => {
        const draft = readDraft(problem, v.language)
        return draft !== null && draft !== v.starter_code
      })
      .map((v) => v.language)
  }, [problem, variant?.language])
  const currentEdited = !!variant && code !== variant.starter_code
  const edited = useMemo(
    () => new Set<Language>(currentEdited && variant ? [...editedElsewhere, variant.language] : editedElsewhere),
    [editedElsewhere, currentEdited, variant],
  )

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

  // A submission is in flight from the moment it has an id until a terminal status arrives,
  // *including* the gap before its first poll returns, when the new query has no data yet.
  // Counting only `submission`'s status made `judging` drop to false for that one round
  // trip, so the Results pane flashed its empty prompt and Run/Submit re-enabled before
  // flipping back to "Judging…" (a visible jitter on every Run and Submit). A first poll
  // that never succeeds is still bounded: the deadline below times it out the same way.
  const awaitingFirstPoll = !!submissionId && !submission
  const stillJudging = awaitingFirstPoll || (!!submission && !isTerminal(submission.status))
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
      const res = await api.post<{ id: string }>(path, {
        problem_id: problem.id,
        code,
        language: variant?.language,
      })
      setSubmissionId(res.id)
    } catch (e) {
      setActionError(e instanceof HttpError ? e.message : 'Failed to submit.')
    } finally {
      setStarting(false)
    }
  }

  /** Reset only the current language; other languages' drafts are untouched. */
  function resetCode() {
    if (!problem || !variant) return
    setCode(variant.starter_code)
    localStorage.removeItem(draftKey(problem.slug, variant.language))
  }

  /** Load code (a solution, a past submission) into `into`'s editor, switching
   * to that language first when it isn't the current one. Like `switchLanguage`,
   * it won't change the language while a verdict is pending. */
  function loadCode(c: string, into: Language) {
    if (!problem || (judging && into !== variant?.language)) return
    const v = problem.languages.find((l) => l.language === into)
    if (!v) return
    const next = withReferenceComment(c, v.starter_code, into)
    setLanguage(into)
    setCode(next)
    localStorage.setItem(draftKey(problem.slug, into), next)
    localStorage.setItem(languageKey(problem.slug), into)
  }

  if (isLoading) return <div className="p-6 text-sm text-zinc-500">Loading…</div>
  if (isError || !problem || !variant) return <div className="p-6 text-sm text-rose-400 light:text-rose-600">Problem not found.</div>

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
                  {/* The statement is shared by every language; this is the one
                      language-specific addendum ("use i64", …), shown for the
                      language in the editor. */}
                  {variant.note_md && (
                    <aside
                      aria-label={`${LANGUAGE_LABEL[variant.language]} note`}
                      className="rounded-r border-l-2 border-indigo-500 bg-zinc-900/60 px-3 py-2 text-sm"
                    >
                      <div className="mb-0.5 text-[11px] font-semibold uppercase tracking-wide text-indigo-400 light:text-indigo-600">
                        {LANGUAGE_LABEL[variant.language]} note
                      </div>
                      <div className={`prose prose-sm max-w-none ${proseInvert}`}>
                        <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
                          {variant.note_md}
                        </ReactMarkdown>
                      </div>
                    </aside>
                  )}
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
                            <span className="text-zinc-300">{formatInput(sc.input, variant.params, problem.kind)}</span>
                          </div>
                          <div>
                            <span className="text-zinc-500">Output: </span>
                            <span className="text-zinc-300">{JSON.stringify(sc.expected)}</span>
                          </div>
                          {/* A TreeNode/ListNode/GraphNode case's arrays above are
                              the judge's wire format, not something you can read a
                              shape out of — the diagram is the shape. */}
                          <SampleDiagrams problem={problem} variant={variant} sample={sc} />
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
                  language={variant.language}
                  languages={problem.languages.map((v) => v.language)}
                  onLoadCode={loadCode}
                  onSwitchLanguage={switchLanguage}
                  languageLocked={judging}
                />
              ) : (
                <Submissions
                  slug={problem.slug}
                  multiLanguage={multiLanguage}
                  onLoadCode={loadCode}
                  lockedTo={judging ? variant.language : null}
                />
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
                {/* Wraps: with a language switcher, the header can outgrow a phone's
                    width, and the card clips overflow (Submit would be cut off). */}
                <header className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 border-b border-zinc-800 px-3 py-1.5 text-xs">
                  {multiLanguage ? (
                    <LanguageSwitcher
                      languages={problem.languages.map((v) => v.language)}
                      value={variant.language}
                      edited={edited}
                      disabled={judging}
                      onChange={switchLanguage}
                    />
                  ) : (
                    <span className="text-zinc-500">{LANGUAGE_LABEL[variant.language]}</span>
                  )}
                  <div className="ml-auto flex items-center gap-2">
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
                  {/* One Monaco model per language (`path`), so each keeps its own
                      undo history: with one shared model, Ctrl+Z after a switch
                      restored the other language's code into this draft. */}
                  <Editor
                    path={`${problem.slug}/${variant.language}`}
                    height="100%"
                    language={MONACO_LANGUAGE[variant.language]}
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
          multiLanguage={multiLanguage}
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
