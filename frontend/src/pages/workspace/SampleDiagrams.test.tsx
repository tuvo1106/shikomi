import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ProblemDetail, SampleCase } from '../../api/types'
import { SampleDiagrams } from './SampleDiagrams'

const PROBLEM = {
  slug: 'mirror-tree',
  kind: 'function',
  params: [{ name: 'root', type: 'TreeNode' }],
  return_type: 'TreeNode',
} as ProblemDetail

const sample = (input: unknown[], expected: unknown): SampleCase => ({ ordinal: 0, input, expected })

/** `<text>` x-position per node label, which is what "leans left/right" means. */
function positions(container: HTMLElement): Record<string, number> {
  return Object.fromEntries(
    [...container.querySelectorAll('text')].map((t) => [t.textContent!, Number(t.getAttribute('x'))]),
  )
}

describe('SampleDiagrams', () => {
  it('draws one labeled diagram per node-typed input plus the expected output', () => {
    render(<SampleDiagrams problem={PROBLEM} sample={sample([[1, 2, 3]], [1, 3, 2])} />)
    expect(screen.getByText('root')).toBeInTheDocument()
    expect(screen.getByText('output')).toBeInTheDocument()
    expect(screen.getAllByRole('img')).toHaveLength(2)
  })

  it('renders nothing for a problem with no node-typed params', () => {
    const pairSum = { ...PROBLEM, params: [{ name: 'nums', type: 'List[int]' }], return_type: '' } as ProblemDetail
    const { container } = render(<SampleDiagrams problem={pairSum} sample={sample([[1, 6]], [0, 1])} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for an empty structure rather than an empty box', () => {
    const { container } = render(<SampleDiagrams problem={PROBLEM} sample={sample([[]], [])} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('leans a lone child to its own side, so [1,2] and [1,null,2] differ', () => {
    // The whole reason this is hand-drawn: a parent centered over its only
    // child would render these two trees identically.
    const left = render(<SampleDiagrams problem={PROBLEM} sample={sample([[1, 2]], null)} />)
    expect(positions(left.container)['2']).toBeLessThan(positions(left.container)['1'])
    const right = render(<SampleDiagrams problem={PROBLEM} sample={sample([[1, null, 2]], null)} />)
    expect(positions(right.container)['2']).toBeGreaterThan(positions(right.container)['1'])
  })

  it('labels each structure of a List[...] param separately', () => {
    const mergeK = {
      ...PROBLEM,
      params: [{ name: 'lists', type: 'List[ListNode]' }],
      return_type: 'ListNode',
    } as ProblemDetail
    render(<SampleDiagrams problem={mergeK} sample={sample([[[1, 4], [2, 6]]], [1, 2, 4, 6])} />)
    expect(screen.getByText('lists[0]')).toBeInTheDocument()
    expect(screen.getByText('lists[1]')).toBeInTheDocument()
  })
})
