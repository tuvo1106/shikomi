import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../../api/client'
import { Modal } from '../../components/Modal'
import type { ProblemListItem, RuntimeDistribution, Submission } from '../../api/types'
import { Confetti } from './Confetti'
import { Histogram } from './Histogram'

/**
 * The celebration shown after an accepted Submit: a confetti burst, the test-case
 * tally, runtime, "beats X%", a runtime histogram positioning this submission
 * against everyone else's, and a next-problem call to action. Fetches the
 * distribution and the next problem lazily (only when the modal exists) and hides
 * the chart when there's too little data to be meaningful.
 *
 * "Next problem" is what `GET /problems/{slug}/next` says: the next unsolved
 * problem after this one in list order (wrapping at the end), decided
 * server-side so it matches the list's own ordering. If there's none (everything
 * else solved) it falls back to the problem list, so the
 * call to action is never a dead end.
 */
export function SuccessModal({
  submission,
  problemSlug,
  onClose,
  onViewSubmissions,
}: {
  submission: Submission
  problemSlug: string
  onClose: () => void
  onViewSubmissions: () => void
}) {
  const { data: dist } = useQuery({
    queryKey: ['distribution', submission.id],
    queryFn: () => api.get<RuntimeDistribution>(`/submissions/${submission.id}/distribution`),
  })
  // Keyed by submission too: what's "next" depends on what's solved, and this
  // submission just changed that, so an older answer for the same slug is stale.
  const { data: next } = useQuery({
    queryKey: ['next-problem', problemSlug, submission.id],
    queryFn: () => api.get<ProblemListItem | null>(`/problems/${problemSlug}/next`),
  })
  const verdict = submission.verdict_detail
  return (
    <>
      <Confetti />
      <Modal label="Accepted" onClose={onClose} className="max-w-sm p-6 text-center">
        <div className="text-4xl">🎉</div>
        <h2 className="mt-2 text-lg font-semibold text-emerald-400 light:text-emerald-600">Accepted</h2>
        <div className="mt-3 space-y-1 text-sm text-zinc-300">
          {verdict && verdict.total > 0 && (
            <div>
              <span className="font-mono">
                {verdict.passed}/{verdict.total}
              </span>{' '}
              test cases passed
            </div>
          )}
          {submission.runtime_ms != null && (
            <div>
              Runtime <span className="font-mono">{submission.runtime_ms} ms</span>
            </div>
          )}
          <div className="text-zinc-400">
            {submission.runtime_percentile != null
              ? `Beats ${submission.runtime_percentile}% of accepted submissions`
              : "You're the first accepted submission!"}
          </div>
        </div>
        {dist && dist.total > 1 && <Histogram dist={dist} runtime={submission.runtime_ms} />}
        <div className="mt-5 space-y-2">
          {/* Dismiss on the way out; the next problem's Workspace remounts (it's keyed by
              slug), so nothing carries over, but there's no reason to leave this open. */}
          <Link
            to={next ? `/problems/${next.slug}` : '/problems'}
            onClick={onClose}
            className="block rounded bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400"
          >
            {next ? `Next problem: ${next.title} →` : 'Back to problems'}
          </Link>
          <div className="flex gap-2">
            <button
              onClick={onViewSubmissions}
              className="flex-1 rounded border border-zinc-700 px-3 py-1.5 text-sm text-zinc-200 hover:bg-zinc-800"
            >
              View submissions
            </button>
            <button
              onClick={onClose}
              className="flex-1 rounded border border-zinc-700 px-3 py-1.5 text-sm text-zinc-200 hover:bg-zinc-800"
            >
              Close
            </button>
          </div>
        </div>
      </Modal>
    </>
  )
}
