import { useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { Difficulty, ProblemFacets, ProblemListResponse, UserStatus } from '../api/types'
import { DifficultyBadge, StatusBadge } from '../components/badges'

/** The page size this client asks for — sent explicitly on every request (see
 * the `page_size` param below) rather than relying on it happening to match
 * the server's own default, so the two can't silently drift apart. */
const PAGE_SIZE = 25

// Delay before free-text search enters the query key, so typing doesn't fire a
// request per keystroke.
const SEARCH_DEBOUNCE_MS = 300

// A plain string option shows itself as both value and label (difficulty/
// status/tag, capitalized via CSS below); a {value, label} pair lets a fixed
// curated set (collection) show a friendly label ("Gang Of Four") while
// filtering on a different wire value ("gang-of-four").
type SelectOption<T extends string> = T | { value: T; label: string }

function Select<T extends string>({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: T | ''
  onChange: (v: T | '') => void
  options: SelectOption<T>[]
  placeholder: string
}) {
  const normalized = options.map((o) => (typeof o === 'string' ? { value: o, label: o } : o))
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as T | '')}
      className="min-w-[9rem] rounded-md border border-zinc-800 bg-zinc-900 px-2 py-1.5 text-sm capitalize text-zinc-200 outline-none focus:border-indigo-500"
    >
      <option value="">{placeholder}</option>
      {normalized.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

/** Display name for an operator-defined collection slug ("gang-of-four" →
 * "Gang Of Four"). Collections are free-form, so there's no label table to keep
 * in sync with whatever an operator seeds; the list itself comes from the
 * facets query. */
function collectionLabel(slug: string): string {
  return slug
    .split('-')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

function ListSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="h-9 animate-pulse rounded bg-zinc-900" />
      ))}
    </div>
  )
}

/**
 * The problem catalog (the app home for signed-in users): a filterable/searchable
 * table. Filters (difficulty/status/tag/collection) are folded into the query key
 * so changing one refetches, and the server returns each row's solved/attempted/
 * unsolved status. Free-text search is debounced (see `SEARCH_DEBOUNCE_MS`) before
 * it enters the query key, so typing doesn't fire a request per keystroke. The Tag
 * filter is single-select (see `tagOptions`), matching the server's single-tag
 * `?tag=` filter — Collection is single-select the same way, against `?collection=`.
 */
export default function Problems() {
  const [difficulty, setDifficulty] = useState<'' | Difficulty>('')
  const [status, setStatus] = useState<'' | UserStatus>('')
  const [tag, setTag] = useState('')
  const [collection, setCollection] = useState('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [page, setPage] = useState(1)

  // Only the settled value enters the query key — see SEARCH_DEBOUNCE_MS above.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(id)
  }, [search])

  // Any filter change re-scopes the result set, so an old page number would point
  // at the wrong (or an empty) slice — snap back to page 1 whenever a filter moves.
  useEffect(() => setPage(1), [difficulty, status, tag, collection, debouncedSearch])

  const params = new URLSearchParams()
  if (difficulty) params.set('difficulty', difficulty)
  if (status) params.set('status', status)
  if (tag) params.set('tag', tag)
  if (collection) params.set('collection', collection)
  if (debouncedSearch) params.set('search', debouncedSearch)
  if (page > 1) params.set('page', String(page))
  params.set('page_size', String(PAGE_SIZE))
  const qs = params.toString()

  const { data, isLoading, isError } = useQuery({
    queryKey: ['problems', difficulty, status, tag, collection, debouncedSearch, page],
    queryFn: () => api.get<ProblemListResponse>(`/problems${qs ? `?${qs}` : ''}`),
    // Keep the current page visible while the next one loads — no flash of the
    // skeleton on every Prev/Next click.
    placeholderData: keepPreviousData,
  })

  // Filter vocabularies come from /problems/facets, not from the loaded page:
  // the catalog can span many pages, so deriving them from `data.items` would
  // make any tag absent from the current page unselectable. Cached hard because
  // the vocabulary changes only when problems are seeded.
  const { data: facets } = useQuery({
    queryKey: ['problem-facets'],
    queryFn: () => api.get<ProblemFacets>('/problems/facets'),
    staleTime: 5 * 60 * 1000,
  })

  const tagOptions = facets?.tags ?? []
  const collectionOptions = useMemo(
    () =>
      (facets?.collections ?? []).map((value) => ({
        value,
        label: collectionLabel(value),
      })),
    [facets],
  )

  const total = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search problems…"
          className="rounded-md border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-100 outline-none focus:border-indigo-500"
        />
        <Select value={difficulty} onChange={setDifficulty} options={['easy', 'medium', 'hard']} placeholder="Difficulty" />
        <Select value={status} onChange={setStatus} options={['solved', 'attempted', 'unsolved']} placeholder="Status" />
        <Select value={tag} onChange={setTag} options={tagOptions} placeholder="Tag" />
        <Select value={collection} onChange={setCollection} options={collectionOptions} placeholder="Collection" />
      </div>

      {isLoading && <ListSkeleton />}
      {isError && <div className="text-sm text-rose-400 light:text-rose-600">Failed to load problems.</div>}

      {data && (
        <table className="w-full table-fixed text-sm">
          <colgroup>
            <col className="w-10" />
            <col />
            <col className="w-24" />
            <col className="hidden w-56 sm:table-column" />
          </colgroup>
          <thead className="text-left text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-3 py-2 font-medium"></th>
              <th className="px-3 py-2 font-medium">Title</th>
              <th className="px-3 py-2 font-medium">Difficulty</th>
              <th className="hidden px-3 py-2 font-medium sm:table-cell">Tags</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((p) => (
              <tr key={p.id} className="border-t border-zinc-800/70 hover:bg-zinc-900/50">
                <td className="px-3 py-2 text-center">
                  <StatusBadge value={p.user_status} />
                </td>
                <td className="px-3 py-2">
                  <Link to={`/problems/${p.slug}`} className="text-zinc-100 hover:text-indigo-400">
                    {p.title}
                  </Link>
                </td>
                <td className="px-3 py-2">
                  <DifficultyBadge value={p.difficulty} />
                </td>
                <td className="hidden truncate px-3 py-2 text-zinc-500 sm:table-cell">
                  {p.tags.slice().sort().join(', ')}
                </td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={4} className="py-10 text-center text-zinc-500">
                  No problems match your filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {totalPages > 1 && (
        <div className="mt-4 flex items-center justify-between text-sm text-zinc-400">
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="rounded-md border border-zinc-800 px-3 py-1.5 hover:border-zinc-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Previous
          </button>
          <span>
            Page {page} of {totalPages}
          </span>
          <button
            type="button"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="rounded-md border border-zinc-800 px-3 py-1.5 hover:border-zinc-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
          </button>
        </div>
      )}
    </div>
  )
}
