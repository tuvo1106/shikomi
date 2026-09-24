import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { json } from '../../test/http'
import { POLL_DEADLINE_MS } from '../../lib/verdict'

// Monaco needs canvas/CDN; stub it. react-markdown is passed through.
vi.mock('@monaco-editor/react', () => ({
  default: ({ value }: { value: string }) => <textarea data-testid="editor" defaultValue={value} readOnly />,
}))
vi.mock('react-markdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}))

import Workspace from './Workspace'

const PROBLEM = {
  id: 'p1',
  slug: 'pair-sum',
  title: 'Pair Sum',
  difficulty: 'easy',
  statement_md: 'Add two numbers.',
  starter_code: 'def pair_sum(): ...',
  function_name: 'pair_sum',
  params: [],
  tags: ['array'],
  constraints: ['`2 <= nums.length <= 10^4`'],
  sample_cases: [],
  has_solutions: false,
  user_status: 'unsolved',
}

function renderWorkspace() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/problems/pair-sum']}>
        <Routes>
          <Route path="/problems/:slug" element={<Workspace />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Workspace', () => {
  it('submits and renders the verdict', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(PROBLEM))
        if (url.endsWith('/submissions') && init?.method === 'POST')
          return Promise.resolve(json({ id: 'sub1', status: 'pending' }, 202))
        if (url.includes('/submissions/sub1'))
          return Promise.resolve(
            json({
              id: 'sub1',
              problem_id: 'p1',
              status: 'accepted',
              is_run: false,
              runtime_ms: 5,
              created_at: 'now',
              verdict_detail: {
                results: [{ test_case_id: 0, status: 'passed', runtime_ms: 5 }],
                passed: 2,
                total: 2,
              },
            }),
          )
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()

    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Submit'))
    // success modal appears on accept
    expect(await screen.findByText('View submissions')).toBeInTheDocument()
    expect(screen.getByText('2/2 passed')).toBeInTheDocument()
  })

  it('shows submission history and opens a submission’s code', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum/submissions'))
          return Promise.resolve(
            json({
              items: [
                { id: 's1', status: 'accepted', runtime_ms: 5, created_at: new Date().toISOString() },
                { id: 's2', status: 'wrong_answer', runtime_ms: null, created_at: new Date().toISOString() },
              ],
            }),
          )
        if (url.includes('/submissions/s1'))
          return Promise.resolve(
            json({
              id: 's1',
              problem_id: 'p1',
              status: 'accepted',
              code: 'print("hi")',
              verdict_detail: null,
              runtime_ms: 5,
              is_run: false,
              created_at: new Date().toISOString(),
            }),
          )
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(PROBLEM))
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()

    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Submissions'))
    expect(await screen.findByText('Wrong Answer')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Accepted')) // click the accepted row
    expect(await screen.findByText('Load into editor')).toBeInTheDocument()
  })

  it('keeps the starter code\'s reference comment when loading a submission into the editor', async () => {
    const listNodeProblem = {
      ...PROBLEM,
      starter_code: '# Definition for singly-linked list.\n# class ListNode:\n#     pass\n\ndef f(): ...',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum/submissions'))
          return Promise.resolve(
            json({ items: [{ id: 's1', status: 'accepted', runtime_ms: 5, created_at: new Date().toISOString() }] }),
          )
        if (url.includes('/submissions/s1'))
          return Promise.resolve(
            json({
              id: 's1',
              problem_id: 'p1',
              status: 'accepted',
              code: 'def f():\n    return None',
              verdict_detail: null,
              runtime_ms: 5,
              is_run: false,
              created_at: new Date().toISOString(),
            }),
          )
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(listNodeProblem))
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()

    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Submissions'))
    await userEvent.click(await screen.findByText('Accepted'))
    await userEvent.click(await screen.findByText('Load into editor'))

    const editor = await screen.findByTestId('editor')
    expect(editor).toHaveValue(
      '# Definition for singly-linked list.\n# class ListNode:\n#     pass\n\ndef f():\n    return None',
    )
  })

  it('gates solutions behind a spoiler warning', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum/solutions'))
          return Promise.resolve(
            json({
              items: [
                {
                  id: 's1',
                  ordinal: 0,
                  title: 'Hash Map',
                  intuition_md: 'Use a dict.',
                  algorithm_md: 'Scan once.',
                  code: 'def pair_sum(): ...',
                  time_complexity: 'O(n)',
                  space_complexity: 'O(n)',
                  time_complexity_reason: 'One pass.',
                  space_complexity_reason: 'A map.',
                },
              ],
            }),
          )
        if (url.includes('/problems/pair-sum'))
          return Promise.resolve(json({ ...PROBLEM, has_solutions: true }))
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()

    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Solutions'))
    expect(await screen.findByText(/spoils the problem/i)).toBeInTheDocument()
    await userEvent.click(screen.getByText('Show solutions'))
    expect(await screen.findByText('Hash Map')).toBeInTheDocument()
  })

  it('surfaces a submit error inline', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(PROBLEM))
        if (url.endsWith('/submissions') && init?.method === 'POST')
          return Promise.resolve(json({ detail: 'Too many submissions; slow down.', code: 'RATE_LIMITED' }, 429))
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()

    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Submit'))
    expect(await screen.findByText(/Too many submissions/i)).toBeInTheDocument()
  })
})


describe('Workspace polling deadline', () => {
  afterEach(() => vi.useRealTimers())

  it('stops the spinner and says so when the server never settles the submission', async () => {
    // Only Date and timers are faked; `shouldAdvanceTime` lets user-event and the query client's
    // promises keep flowing while the clock is advanced by hand.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let polls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(PROBLEM))
        if (url.endsWith('/submissions') && init?.method === 'POST')
          return Promise.resolve(json({ id: 'stuck', status: 'pending' }, 202))
        if (url.includes('/submissions/stuck')) {
          polls++
          return Promise.resolve(
            json({ id: 'stuck', problem_id: 'p1', status: 'running', is_run: false, created_at: 'now' }),
          )
        }
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()
    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Submit'))
    expect(await screen.findByText('Judging…', { selector: 'div' })).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_DEADLINE_MS + 2_000)
    })

    expect(await screen.findByText(/taking longer than expected/)).toBeInTheDocument()
    expect(screen.getByText('Submit')).not.toBeDisabled() // the user can try again
    const seen = polls
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(polls).toBe(seen) // and it really stopped asking
  })
})

describe('Workspace across problems', () => {
  it('starts the next problem fresh: description tab, no leftover verdict', async () => {
    const NEXT = { ...PROBLEM, id: 'p2', slug: 'add-two', title: 'Add Two', statement_md: 'Sum stuff.' }
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum/submissions')) return Promise.resolve(json({ items: [] }))
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(PROBLEM))
        if (url.includes('/problems/add-two')) return Promise.resolve(json(NEXT))
        if (url.endsWith('/submissions') && init?.method === 'POST')
          return Promise.resolve(json({ id: 'sub1', status: 'pending' }, 202))
        if (url.includes('/submissions/sub1'))
          return Promise.resolve(
            json({
              id: 'sub1', problem_id: 'p1', status: 'accepted', is_run: false, runtime_ms: 5,
              created_at: 'now',
              verdict_detail: { results: [], passed: 2, total: 2 },
            }),
          )
        if (url.includes('/problems?')) return Promise.resolve(json({ items: [], total: 0 }))
        return Promise.resolve(json({}, 404))
      }),
    )
    function GoNext() {
      const navigate = useNavigate()
      return <button onClick={() => navigate('/problems/add-two')}>go-next</button>
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/problems/pair-sum']}>
          <GoNext />
          <Routes>
            <Route path="/problems/:slug" element={<Workspace />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Submit'))
    expect(await screen.findByText('2/2 passed')).toBeInTheDocument()
    await userEvent.click(await screen.findByRole('button', { name: 'Close' }))
    await userEvent.click(screen.getByText('Submissions')) // leave the description tab

    await userEvent.click(screen.getByText('go-next'))

    expect(await screen.findByText('Add Two')).toBeInTheDocument()
    expect(screen.getByText('Sum stuff.')).toBeInTheDocument() // back on the description tab
    expect(screen.queryByText('2/2 passed')).not.toBeInTheDocument() // last verdict is gone
  })
})
