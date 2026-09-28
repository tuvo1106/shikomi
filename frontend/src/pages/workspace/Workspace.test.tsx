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

const variant = (language: string, starter_code: string, note_md = '') => ({
  language,
  starter_code,
  function_name: 'pair_sum',
  class_name: null,
  params: [],
  return_type: '',
  note_md,
})

const PROBLEM = {
  id: 'p1',
  slug: 'pair-sum',
  title: 'Pair Sum',
  difficulty: 'easy',
  statement_md: 'Add two numbers.',
  kind: 'function',
  languages: [variant('python', 'def pair_sum(): ...')],
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
              problem_id: 'p1', language: 'python',
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
                { id: 's1', status: 'accepted', language: 'python', runtime_ms: 5, created_at: new Date().toISOString() },
                { id: 's2', status: 'wrong_answer', language: 'python', runtime_ms: null, created_at: new Date().toISOString() },
              ],
            }),
          )
        if (url.includes('/submissions/s1'))
          return Promise.resolve(
            json({
              id: 's1',
              problem_id: 'p1', language: 'python',
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
      languages: [
        variant('python', '# Definition for singly-linked list.\n# class ListNode:\n#     pass\n\ndef f(): ...'),
      ],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum/submissions'))
          return Promise.resolve(
            json({ items: [{ id: 's1', status: 'accepted', language: 'python', runtime_ms: 5, created_at: new Date().toISOString() }] }),
          )
        if (url.includes('/submissions/s1'))
          return Promise.resolve(
            json({
              id: 's1',
              problem_id: 'p1', language: 'python',
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
                  code: { python: 'def pair_sum(): ...' },
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
            json({ id: 'stuck', problem_id: 'p1', language: 'python', status: 'running', is_run: false, created_at: 'now' }),
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
              id: 'sub1', problem_id: 'p1', language: 'python', status: 'accepted', is_run: false, runtime_ms: 5,
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

describe('Workspace judging state', () => {
  afterEach(() => vi.useRealTimers())

  it('stays in "Judging…" between the submit response and the first poll', async () => {
    // The POST answers at once, but the first GET is held open. Before the fix, the
    // new query had no data in that window, so `judging` briefly went false: the
    // pane flashed the empty prompt and the buttons re-enabled, visible as a jitter.
    let releasePoll: (r: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(PROBLEM))
        if (url.endsWith('/submissions') && init?.method === 'POST')
          return Promise.resolve(json({ id: 'slow', status: 'pending' }, 202))
        if (url.includes('/submissions/slow'))
          return new Promise<Response>((resolve) => {
            releasePoll = resolve
          })
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()
    expect(await screen.findByText('Pair Sum')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Submit'))
    // Wait until the first poll is actually in flight, i.e. the POST has resolved.
    await vi.waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.some(([u]) => String(u).includes('/submissions/slow'))).toBe(true),
    )

    expect(screen.getByText('Judging…', { selector: 'div' })).toBeInTheDocument()
    expect(screen.queryByText(/Run against sample cases/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Judging…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()

    await act(async () => {
      releasePoll(
        json({
          id: 'slow', problem_id: 'p1', language: 'python', status: 'wrong_answer', is_run: false, created_at: 'now',
          verdict_detail: { results: [{ test_case_id: 0, status: 'wrong_answer', runtime_ms: 1 }], passed: 0, total: 1 },
        }),
      )
    })
    expect(await screen.findByText('Wrong Answer')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Submit' })).not.toBeDisabled()
  })

  it('still gives up at the deadline when the first poll never succeeds', async () => {
    // Holding "Judging…" until the first poll has data must not become a hang if
    // that poll fails: the polling deadline covers this case too.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(PROBLEM))
        if (url.endsWith('/submissions') && init?.method === 'POST')
          return Promise.resolve(json({ id: 'broken', status: 'pending' }, 202))
        if (url.includes('/submissions/broken')) return Promise.resolve(json({ detail: 'boom' }, 500))
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
    expect(screen.getByText('Submit')).not.toBeDisabled()
  })
})

describe('Workspace languages', () => {
  const MULTI = {
    ...PROBLEM,
    languages: [
      variant('python', 'def pair_sum(): ...'),
      variant('js', 'var pairSum = function() {};'),
      variant('rust', 'fn pair_sum() {}', 'Use `i64`.'),
    ],
  }

  /** Serve `problem`; record every Run/Submit body so a test can read the language sent. */
  function serve(problem: object) {
    const posted: Array<Record<string, unknown>> = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (init?.method === 'POST') {
          posted.push(JSON.parse(String(init.body)))
          return Promise.resolve(json({ id: 'sub1', status: 'pending' }, 202))
        }
        if (url.includes('/submissions/sub1'))
          return Promise.resolve(
            json({ id: 'sub1', problem_id: 'p1', language: 'rust', status: 'wrong_answer', is_run: true,
              code: '', created_at: 'now', verdict_detail: { results: [], passed: 0, total: 1 } }),
          )
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json(problem))
        return Promise.resolve(json({}, 404))
      }),
    )
    return posted
  }

  it('shows a plain label, not a switcher, for a one-language problem', async () => {
    serve(PROBLEM)
    renderWorkspace()
    expect(await screen.findByText('Python3')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Language' })).not.toBeInTheDocument()
  })

  it('keeps a separate draft per language across switches', async () => {
    localStorage.setItem('code:pair-sum:python', 'my python draft')
    serve(MULTI)
    renderWorkspace()

    const editor = await screen.findByTestId('editor')
    expect(editor).toHaveValue('my python draft')
    expect(screen.getByRole('button', { name: 'Python3' })).toHaveAttribute('aria-pressed', 'true')
    // The dot (in the title) marks the edited draft only.
    expect(screen.getByTitle('Python3 (edited)')).toBeInTheDocument()
    expect(screen.getByTitle('Rust')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Rust' }))
    expect(screen.getByTestId('editor')).toHaveValue('fn pair_sum() {}')
    await userEvent.click(screen.getByRole('button', { name: 'Python3' }))
    expect(screen.getByTestId('editor')).toHaveValue('my python draft')
  })

  it('shows the selected language\'s note and sends its language on Run', async () => {
    const posted = serve(MULTI)
    renderWorkspace()
    await screen.findByText('Pair Sum')
    expect(screen.queryByText('Rust note')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Rust' }))
    expect(screen.getByText('Rust note')).toBeInTheDocument()
    expect(screen.getByText('Use `i64`.')).toBeInTheDocument()

    await userEvent.click(screen.getByText('Run'))
    await screen.findByText('Wrong Answer')
    expect(posted[0]).toMatchObject({ problem_id: 'p1', code: 'fn pair_sum() {}', language: 'rust' })
    // The verdict names the language it was judged in.
    expect(screen.getAllByText('Rust').length).toBeGreaterThan(1)
  })

  it('shows solutions in the editor language and offers to switch when one is missing', async () => {
    const solution = (id: string, title: string, code: Record<string, string>) => ({
      id, ordinal: 0, title, intuition_md: 'i', algorithm_md: '', code,
      time_complexity: 'O(n)', space_complexity: 'O(1)', time_complexity_reason: '', space_complexity_reason: '',
    })
    localStorage.setItem('sol:pair-sum', '1') // past the spoiler gate
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string) => {
        const url = String(input)
        if (url.includes('/problems/pair-sum/solutions'))
          return Promise.resolve(json({ items: [
            solution('a', 'Sweep', { python: 'py sweep', js: 'js sweep', rust: 'rs sweep' }),
            solution('b', 'Compact', { rust: 'rs compact' }),
          ] }))
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json({ ...MULTI, has_solutions: true }))
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()
    await screen.findByText('Pair Sum')
    await userEvent.click(screen.getByText('Solutions'))

    expect(await screen.findByText('py sweep')).toBeInTheDocument()
    expect(screen.getByText('Rust only')).toBeInTheDocument()
    expect(screen.getByText('No Python3 version of this approach.')).toBeInTheDocument()

    await userEvent.click(screen.getByText('Switch to Rust'))
    expect(await screen.findByText('rs compact')).toBeInTheDocument()
    expect(screen.getByTestId('editor')).toHaveValue('fn pair_sum() {}') // the switch is the editor's
  })

  it('reopens in the language last used on the problem', async () => {
    localStorage.setItem('lang:pair-sum', 'js')
    serve(MULTI)
    renderWorkspace()
    expect(await screen.findByTestId('editor')).toHaveValue('var pairSum = function() {};')
  })

  it('moves a draft saved before languages existed, on a one-language problem', async () => {
    localStorage.setItem('code:pair-sum', 'old draft')
    serve(PROBLEM)
    renderWorkspace()
    expect(await screen.findByTestId('editor')).toHaveValue('old draft')
    expect(localStorage.getItem('code:pair-sum:python')).toBe('old draft')
    expect(localStorage.getItem('code:pair-sum')).toBeNull()
  })

  it('leaves a pre-languages draft alone on a multi-language problem', async () => {
    // Its default language may have changed since (merge-booking-windows went from
    // Rust to Python), so filing the draft under the default could submit Rust as Python.
    localStorage.setItem('code:pair-sum', 'fn old() {}')
    serve(MULTI)
    renderWorkspace()
    expect(await screen.findByTestId('editor')).toHaveValue('def pair_sum(): ...')
    expect(localStorage.getItem('code:pair-sum:python')).toBeNull()
    expect(localStorage.getItem('code:pair-sum')).toBe('fn old() {}')
  })

  it('locks the language while a verdict is pending, the Solutions tab included', async () => {
    const solution = { id: 'b', ordinal: 0, title: 'Compact', intuition_md: 'i', algorithm_md: '',
      code: { rust: 'rs compact' }, time_complexity: 'O(n)', space_complexity: 'O(1)',
      time_complexity_reason: '', space_complexity_reason: '' }
    localStorage.setItem('sol:pair-sum', '1')
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => {
        const url = String(input)
        if (init?.method === 'POST') return Promise.resolve(json({ id: 'slow', status: 'pending' }, 202))
        if (url.includes('/submissions/slow')) return new Promise<Response>(() => {}) // never settles
        if (url.includes('/problems/pair-sum/solutions')) return Promise.resolve(json({ items: [solution] }))
        if (url.includes('/problems/pair-sum')) return Promise.resolve(json({ ...MULTI, has_solutions: true }))
        return Promise.resolve(json({}, 404))
      }),
    )
    renderWorkspace()
    await screen.findByText('Pair Sum')
    await userEvent.click(screen.getByText('Submit'))
    await userEvent.click(screen.getByText('Solutions'))

    const switchButton = await screen.findByText('Switch to Rust')
    expect(switchButton).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Rust' })).toBeDisabled() // the switcher too
    expect(screen.getByTestId('editor')).toHaveValue('def pair_sum(): ...')
  })
})
