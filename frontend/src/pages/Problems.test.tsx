import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Problems from './Problems'
import { json } from '../test/http'

function renderProblems() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Problems />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Route the facets request separately from the list request.
 *
 * The page issues two calls — `/problems?...` for the page and
 * `/problems/facets` for the filter vocabularies — so a single blanket stub
 * would feed list JSON to the facets query and leave every dropdown empty. */
function mockApi(
  listResponder: (url: string) => unknown,
  facets: { tags: string[]; collections: string[] } = { tags: [], collections: [] },
) {
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve(json(String(url).includes('/problems/facets') ? facets : listResponder(String(url)))),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('Problems', () => {
  it('renders the problem list from the API', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          json({
            items: [
              { id: '1', slug: 'pair-sum', title: 'Pair Sum', difficulty: 'easy', tags: ['array'], user_status: 'solved' },
              { id: '2', slug: 'design-price-feed', title: 'Price Feed', difficulty: 'medium', tags: ['sorting'], user_status: 'unsolved' },
            ],
            total: 2,
          }),
        ),
      ),
    )
    renderProblems()
    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    expect(screen.getByText('Price Feed')).toBeInTheDocument()
    // "easy"/"medium" appear in both the filter dropdown and the badges.
    expect(screen.getAllByText('easy').length).toBeGreaterThan(0)
    expect(screen.getAllByText('medium').length).toBeGreaterThan(0)
  })

  it('shows an empty state when nothing matches', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json({ items: [], total: 0 }))))
    renderProblems()
    expect(await screen.findByText(/No problems match/i)).toBeInTheDocument()
  })

  it('paginates when total exceeds a page and Next fetches ?page=2', async () => {
    // total (120) > server page_size (25) → controls must appear and reach page 2.
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve(
        json({
          items: [
            {
              id: url.includes('page=2') ? 'b' : 'a',
              slug: url.includes('page=2') ? 'p2-item' : 'p1-item',
              title: url.includes('page=2') ? 'Second Page Item' : 'First Page Item',
              difficulty: 'easy',
              tags: [],
              user_status: 'unsolved',
            },
          ],
          total: 120,
        }),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    renderProblems()

    expect(await screen.findByText('First Page Item')).toBeInTheDocument()
    // Controls render because 120 > 25 (→ 5 pages).
    expect(screen.getByText('Page 1 of 5')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByText('Second Page Item')).toBeInTheDocument()
    expect(screen.getByText('Page 2 of 5')).toBeInTheDocument()
    // The page-2 request carried ?page=2 through to the API.
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).includes('page=2'))).toBe(true),
    )
  })

  it('debounces search input so only one request fires after typing stops', async () => {
    const fetchMock = vi.fn((_url: string) => Promise.resolve(json({ items: [], total: 0 })))
    vi.stubGlobal('fetch', fetchMock)
    renderProblems()
    await screen.findByText(/No problems match/i) // initial load settles

    vi.useFakeTimers()
    try {
      const callsBefore = fetchMock.mock.calls.length
      const input = screen.getByPlaceholderText('Search problems…')
      fireEvent.change(input, { target: { value: 't' } })
      fireEvent.change(input, { target: { value: 'tw' } })
      fireEvent.change(input, { target: { value: 'two' } })

      // Still within the debounce window — no new request yet.
      expect(fetchMock.mock.calls.length).toBe(callsBefore)

      await act(() => vi.advanceTimersByTimeAsync(300))

      expect(fetchMock.mock.calls.length).toBe(callsBefore + 1)
      expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain('search=two')
    } finally {
      vi.useRealTimers()
    }
  })

  it('selecting a tag adds ?tag= to the fetched URL and re-queries', async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.includes('/problems/facets')) {
        return Promise.resolve(json({ tags: ['array', 'hash-table'], collections: [] }))
      }
      const filtered = url.includes('tag=array')
      return Promise.resolve(
        json({
          items: filtered
            ? [{ id: '1', slug: 'pair-sum', title: 'Pair Sum', difficulty: 'easy', tags: ['array'], user_status: 'unsolved' }]
            : [
                { id: '1', slug: 'pair-sum', title: 'Pair Sum', difficulty: 'easy', tags: ['array'], user_status: 'unsolved' },
                { id: '2', slug: 'design-vending-machine', title: 'Vending Machine', difficulty: 'easy', tags: ['hash-table'], user_status: 'unsolved' },
              ],
          total: filtered ? 1 : 2,
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    renderProblems()

    await screen.findByText('Pair Sum')
    expect(screen.getByText('Vending Machine')).toBeInTheDocument()
    await screen.findByRole('option', { name: 'array' })

    fireEvent.change(screen.getByDisplayValue('Tag'), { target: { value: 'array' } })

    await waitFor(() => expect(screen.queryByText('Vending Machine')).not.toBeInTheDocument())
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('tag=array'))).toBe(true)
  })

  it('selecting a collection adds ?collection= to the fetched URL and re-queries', async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.includes('/problems/facets')) {
        return Promise.resolve(json({ tags: [], collections: ['starter', 'gang-of-four'] }))
      }
      const filtered = url.includes('collection=starter')
      return Promise.resolve(
        json({
          items: filtered
            ? [{ id: '1', slug: 'pair-sum', title: 'Pair Sum', difficulty: 'easy', tags: ['array'], user_status: 'unsolved' }]
            : [
                { id: '1', slug: 'pair-sum', title: 'Pair Sum', difficulty: 'easy', tags: ['array'], user_status: 'unsolved' },
                { id: '2', slug: 'design-vending-machine', title: 'Vending Machine', difficulty: 'easy', tags: ['hash-table'], user_status: 'unsolved' },
              ],
          total: filtered ? 1 : 2,
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    renderProblems()

    await screen.findByText('Pair Sum')
    expect(screen.getByText('Vending Machine')).toBeInTheDocument()

    await screen.findByRole('option', { name: 'Starter' })
    fireEvent.change(screen.getByDisplayValue('Collection'), { target: { value: 'starter' } })

    await waitFor(() => expect(screen.queryByText('Vending Machine')).not.toBeInTheDocument())
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('collection=starter'))).toBe(true)
  })

  it('offers filter options absent from the current page (the paging bug)', async () => {
    // Why the facets endpoint exists: dropdowns built from `data.items` would
    // make a tag or collection used only by problems on other pages
    // unselectable. Here the page contains only `array`, while the catalog also
    // has `sql` — which must still be offered.
    const fetchMock = mockApi(
      () => ({
        items: [
          { id: '1', slug: 'pair-sum', title: 'Pair Sum', difficulty: 'easy', tags: ['array'], user_status: 'unsolved' },
        ],
        total: 400,
      }),
      { tags: ['array', 'sql'], collections: ['gang-of-four', 'brand-new-list'] },
    )
    renderProblems()
    await screen.findByText('Pair Sum')

    expect(await screen.findByRole('option', { name: 'sql' })).toBeInTheDocument()
    // Every collection slug is labelled by title-casing it, so an operator's
    // new collection appears without a label table to update.
    expect(screen.getByRole('option', { name: 'Gang Of Four' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Brand New List' })).toBeInTheDocument()

    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/problems/facets'))).toBe(true)
  })
})
