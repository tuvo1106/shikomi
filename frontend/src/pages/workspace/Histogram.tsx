import type { RuntimeDistribution } from '../../api/types'

/**
 * Runtime-distribution bar chart: buckets everyone's accepted runtimes and
 * highlights the bucket this submission falls into (indigo vs zinc). Shared by the
 * accepted-submit celebration (`SuccessModal`) and the past-submission detail modal
 * so both position a runtime against the field identically.
 */
export function Histogram({ dist, runtime }: { dist: RuntimeDistribution; runtime: number | null }) {
  const n = dist.buckets.length
  const width = n > 0 && dist.hi > dist.lo ? (dist.hi - dist.lo) / n : 0
  const yourBucket =
    runtime != null && width > 0 ? Math.min(Math.floor((runtime - dist.lo) / width), n - 1) : 0
  const max = Math.max(...dist.buckets, 1)
  return (
    <div className="mt-4">
      <div className="mb-1 text-xs text-zinc-500">Runtime distribution</div>
      <div className="flex h-16 items-end gap-0.5">
        {dist.buckets.map((count, i) => (
          <div
            key={i}
            title={`${count} submission(s)`}
            style={{ height: `${count > 0 ? Math.max((count / max) * 100, 6) : 1}%` }}
            className={`flex-1 rounded-t ${i === yourBucket ? 'bg-indigo-500' : 'bg-zinc-700'}`}
          />
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-zinc-500">
        <span>{dist.lo} ms</span>
        <span>{dist.hi} ms</span>
      </div>
    </div>
  )
}
