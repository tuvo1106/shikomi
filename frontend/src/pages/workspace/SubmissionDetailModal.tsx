import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Modal } from '../../components/Modal'
import type { Language, RuntimeDistribution, Submission } from '../../api/types'
import { CodeBlock } from './CodeBlock'
import { Histogram } from './Histogram'
import { StatusText } from './StatusText'
import { relativeTime } from './format'

/**
 * Modal opened from a submission-history row: fetches that submission and shows
 * its status, runtime, and code, with a "Load into editor" action to resume it.
 * For an accepted submission it also fetches the runtime distribution and plots
 * this submission against the field (same chart as the accepted-submit modal).
 */
export function SubmissionDetailModal({
  submissionId,
  language,
  onClose,
  onLoadCode,
}: {
  submissionId: string
  language: Language
  onClose: () => void
  onLoadCode: (code: string) => void
}) {
  const { data: sub, isLoading } = useQuery({
    queryKey: ['submission-detail', submissionId],
    queryFn: () => api.get<Submission>(`/submissions/${submissionId}`),
  })
  // Only accepted submissions have a meaningful runtime to position on the chart.
  const { data: dist } = useQuery({
    queryKey: ['distribution', submissionId],
    queryFn: () => api.get<RuntimeDistribution>(`/submissions/${submissionId}/distribution`),
    enabled: sub?.status === 'accepted',
  })
  return (
    <Modal label="Submission details" onClose={onClose} className="max-w-lg p-4">
      {isLoading || !sub ? (
        <div className="text-sm text-zinc-500">Loading…</div>
      ) : (
        <>
          {/* Wraps on narrow screens (a 320px header would otherwise break "0.335 ms"
              and "2s ago" across lines), keeping each fact on one line. */}
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
            <StatusText status={sub.status} />
            {sub.runtime_ms != null && (
              <span className="whitespace-nowrap font-mono text-zinc-500">{sub.runtime_ms} ms</span>
            )}
            <span className="whitespace-nowrap text-zinc-500">{relativeTime(sub.created_at)}</span>
            <button
              onClick={() => onLoadCode(sub.code)}
              className="ml-auto whitespace-nowrap rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
            >
              Load into editor
            </button>
          </div>
          {sub.status === 'accepted' && (
            <>
              <div className="mb-1 text-sm text-zinc-400">
                {sub.runtime_percentile != null
                  ? `Beats ${sub.runtime_percentile}% of accepted submissions`
                  : "You're the first accepted submission!"}
              </div>
              {dist && dist.total > 1 && (
                <div className="mb-3">
                  <Histogram dist={dist} runtime={sub.runtime_ms} />
                </div>
              )}
            </>
          )}
          {/* The panel itself scrolls (see Modal), so the code block doesn't need
              its own viewport-relative height cap. */}
          <CodeBlock code={sub.code} language={language} copyable={false} />
        </>
      )}
    </Modal>
  )
}
