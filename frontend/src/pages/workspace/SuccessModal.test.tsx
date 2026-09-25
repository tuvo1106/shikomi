import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { json } from '../../test/http'
import type { Submission } from '../../api/types'
import { SuccessModal } from './SuccessModal'

const SUB: Submission = {
  id: 's1',
  problem_id: 'p1',
  status: 'accepted',
  code: '',
  is_run: false,
  runtime_ms: 12,
  created_at: 'now',
  runtime_percentile: 80,
  verdict_detail: { results: [], passed: 7, total: 7 },
}

const item = (slug: string, title: string) => ({
  id: slug, slug, title, difficulty: 'easy', tags: [], user_status: 'unsolved',
})

/** Render the modal with `/problems/pair-sum/next` answering `next` (null = none). */
function renderModal(next: ReturnType<typeof item> | null, onClose = vi.fn()) {
  const fetchMock = vi.fn((input: string) =>
    Promise.resolve(
      String(input).endsWith('/problems/pair-sum/next') ? json(next) : json({ buckets: [], total: 0 }),
    ),
  )
  vi.stubGlobal('fetch', fetchMock)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SuccessModal submission={SUB} problemSlug="pair-sum" onClose={onClose} onViewSubmissions={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { onClose, fetchMock }
}

describe('SuccessModal', () => {
  it('shows the test-case tally, runtime and percentile', async () => {
    renderModal(null)
    expect(await screen.findByText('7/7')).toBeInTheDocument()
    expect(screen.getByText(/test cases passed/)).toBeInTheDocument()
    expect(screen.getByText('12 ms')).toBeInTheDocument()
    expect(screen.getByText('Beats 80% of accepted submissions')).toBeInTheDocument()
  })

  it("links to the problem the server says is next, asked about the current one", async () => {
    const { fetchMock } = renderModal(item('add-two', 'Add Two'))
    const link = await screen.findByRole('link', { name: /Next problem: Add Two/ })
    expect(link).toHaveAttribute('href', '/problems/add-two')
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/problems/pair-sum/next'))).toBe(true)
  })

  it('falls back to the problem list when there is no next problem', async () => {
    renderModal(null)
    const link = await screen.findByRole('link', { name: 'Back to problems' })
    expect(link).toHaveAttribute('href', '/problems')
  })

  it('dismisses itself when following the call to action, so it does not follow you to the next problem', async () => {
    const { onClose } = renderModal(item('add-two', 'Add Two'))
    await userEvent.click(await screen.findByRole('link', { name: /Next problem/ }))
    expect(onClose).toHaveBeenCalled()
  })
})
