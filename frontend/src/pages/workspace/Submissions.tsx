import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { api } from '../../api/client'
import type { Language, SubmissionListResponse } from '../../api/types'
import { ConfirmModal } from '../../components/ConfirmModal'
import { StatusText } from './StatusText'
import { SubmissionDetailModal } from './SubmissionDetailModal'
import { relativeTime } from './format'

export function Submissions({
  slug,
  language,
  onLoadCode,
}: {
  slug: string
  language: Language
  onLoadCode: (code: string) => void
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['submissions', slug],
    queryFn: () => api.get<SubmissionListResponse>(`/problems/${slug}/submissions`),
  })

  async function confirmDelete() {
    if (!pendingDelete) return
    await api.del(`/submissions/${pendingDelete}`)
    queryClient.invalidateQueries({ queryKey: ['submissions', slug] })
    setPendingDelete(null)
  }

  if (isLoading) return <div className="text-sm text-zinc-500">Loading…</div>
  if (!data || data.items.length === 0)
    return <div className="text-sm text-zinc-600">No submissions yet — Submit to see your history.</div>

  return (
    <>
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-zinc-500">
          <tr>
            <th className="py-1 font-medium">Status</th>
            <th className="py-1 font-medium">Runtime</th>
            <th className="py-1 font-medium">Submitted</th>
            <th className="py-1"></th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((s) => (
            <tr
              key={s.id}
              onClick={() => setSelectedId(s.id)}
              className="cursor-pointer border-t border-zinc-800/70 hover:bg-zinc-900/60"
            >
              <td className="py-1.5">
                <StatusText status={s.status} />
              </td>
              <td className="py-1.5 font-mono text-zinc-400">
                {s.runtime_ms != null ? `${s.runtime_ms} ms` : '—'}
              </td>
              <td className="py-1.5 text-zinc-500">{relativeTime(s.created_at)}</td>
              <td className="py-1.5 pr-2 text-right">
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setPendingDelete(s.id)
                  }}
                  title="Delete submission"
                  className="text-zinc-600 hover:text-rose-400"
                >
                  <Trash2 size={14} strokeWidth={1.5} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {selectedId && (
        <SubmissionDetailModal
          submissionId={selectedId}
          language={language}
          onClose={() => setSelectedId(null)}
          onLoadCode={(code) => {
            onLoadCode(code)
            setSelectedId(null)
          }}
        />
      )}
      {pendingDelete && (
        <ConfirmModal
          message="Delete this submission? This can’t be undone."
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </>
  )
}
