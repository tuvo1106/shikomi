import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import katex from 'katex'
import { api } from '../../api/client'
import type { Language, Solution, SolutionsResponse } from '../../api/types'
import { CodeBlock } from './CodeBlock'
import { SectionLabel } from './ui'
import { useTheme } from '../../lib/theme'
import { LANGUAGE_LABEL } from '../../lib/languages'

/**
 * The Solutions tab, behind a spoiler soft-gate: it warns before revealing
 * editorials to someone who hasn't solved the problem, but doesn't hard-block
 * (a nudge, not a wall — solved users or those who previously confirmed skip it).
 * The solutions query is disabled until revealed, so we don't even fetch spoilers
 * the user hasn't asked to see.
 *
 * Code follows the editor's language (`language`); the prose and complexity are
 * shared. An approach without code in that language says which languages it
 * does have and offers to switch, rather than silently showing another
 * language's code.
 */
export function Solutions({
  slug,
  solved,
  language,
  languages,
  onLoadCode,
  onSwitchLanguage,
  languageLocked = false,
}: {
  slug: string
  solved: boolean
  language: Language
  /** Every language the problem offers, default first. */
  languages: Language[]
  onLoadCode: (code: string, language: Language) => void
  onSwitchLanguage: (language: Language) => void
  /** A verdict is pending, so the "Switch to" buttons are disabled (the result
   * belongs to the language it was submitted in). */
  languageLocked?: boolean
}) {
  // Soft gate (§6.3): spoiler warning unless already solved or previously confirmed.
  const [revealed, setRevealed] = useState(
    () => solved || localStorage.getItem(`sol:${slug}`) === '1',
  )
  const { data, isLoading } = useQuery({
    queryKey: ['solutions', slug],
    queryFn: () => api.get<SolutionsResponse>(`/problems/${slug}/solutions`),
    enabled: revealed,
  })

  if (!revealed) {
    return (
      <div className="flex flex-col items-center gap-3 py-12 text-center">
        <p className="max-w-xs text-sm text-zinc-400">
          Viewing the solution before solving spoils the problem. Show it anyway?
        </p>
        <button
          onClick={() => {
            localStorage.setItem(`sol:${slug}`, '1')
            setRevealed(true)
          }}
          className="rounded bg-indigo-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-400"
        >
          Show solutions
        </button>
      </div>
    )
  }

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>
  return (
    <div className="space-y-8">
      {data?.items.map((s) => (
        <SolutionView
          key={s.id}
          solution={s}
          language={language}
          languages={languages}
          onLoadCode={onLoadCode}
          onSwitchLanguage={onSwitchLanguage}
          languageLocked={languageLocked}
        />
      ))}
    </div>
  )
}

function InlineMath({ math }: { math: string }) {
  const html = useMemo(() => katex.renderToString(math, { throwOnError: false }), [math])
  return <span dangerouslySetInnerHTML={{ __html: html }} />
}

/**
 * Renders one prose block of a solution write-up (Intuition, Algorithm).
 *
 * Math gets the same treatment as the problem statement: these blocks quote
 * complexities inline ($O(n \log n)$) and read as broken text without KaTeX.
 */
function MarkdownSection({ label, md }: { label: string; md: string }) {
  const { theme } = useTheme()
  return (
    <div>
      <SectionLabel>{label}</SectionLabel>
      <article className={`prose prose-sm max-w-none ${theme === 'dark' ? 'prose-invert' : ''}`}>
        <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
          {md}
        </ReactMarkdown>
      </article>
    </div>
  )
}

function SolutionView({
  solution,
  language,
  languages,
  onLoadCode,
  onSwitchLanguage,
  languageLocked,
}: {
  solution: Solution
  language: Language
  languages: Language[]
  onLoadCode: (code: string, language: Language) => void
  onSwitchLanguage: (language: Language) => void
  languageLocked: boolean
}) {
  const code = solution.code[language]
  // In the problem's order, so "Rust only" and the switch buttons read consistently.
  const covered = languages.filter((l) => solution.code[l] !== undefined)
  const partial = languages.length > 1 && covered.length < languages.length
  return (
    <div className="space-y-3">
      <h3 className="flex flex-wrap items-baseline gap-2 text-base font-semibold text-zinc-100">
        {solution.title}
        {partial && (
          <span className="text-xs font-normal text-zinc-500">
            {covered.map((l) => LANGUAGE_LABEL[l]).join(', ')} only
          </span>
        )}
      </h3>
      <MarkdownSection label="Intuition" md={solution.intuition_md} />
      {solution.algorithm_md && <MarkdownSection label="Algorithm" md={solution.algorithm_md} />}
      <div>
        <div className="mb-1 flex items-center justify-between">
          <SectionLabel>Implementation</SectionLabel>
          {code !== undefined && (
            <button
              onClick={() => onLoadCode(code, language)}
              className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800"
            >
              Load into editor
            </button>
          )}
        </div>
        {code !== undefined ? (
          <CodeBlock code={code} language={language} copyable={false} />
        ) : (
          <div className="flex flex-wrap items-center gap-2 rounded border border-zinc-800 bg-zinc-900/40 p-2 text-sm text-zinc-500">
            No {LANGUAGE_LABEL[language]} version of this approach.
            {covered.map((l) => (
              <button
                key={l}
                onClick={() => onSwitchLanguage(l)}
                disabled={languageLocked}
                title={languageLocked ? 'Wait for the verdict to switch languages' : undefined}
                className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Switch to {LANGUAGE_LABEL[l]}
              </button>
            ))}
          </div>
        )}
      </div>
      <div>
        <SectionLabel>Complexity Analysis</SectionLabel>
        <div className="space-y-1 text-sm text-zinc-300">
          <div>
            <span className="text-zinc-500">Time: </span>
            <InlineMath math={solution.time_complexity} />
            {solution.time_complexity_reason && (
              <span className="text-zinc-500"> — {solution.time_complexity_reason}</span>
            )}
          </div>
          <div>
            <span className="text-zinc-500">Space: </span>
            <InlineMath math={solution.space_complexity} />
            {solution.space_complexity_reason && (
              <span className="text-zinc-500"> — {solution.space_complexity_reason}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

