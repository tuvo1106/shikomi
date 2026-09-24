import { describe, expect, it } from 'vitest'
import { decodeExpected, decodeInput, isNodeType, nodeParams } from './nodeGraph'

/** Edges as `from-kind->to` label pairs, for readable assertions. */
function edges(graphs: ReturnType<typeof decodeInput>, i = 0) {
  const g = graphs[i]
  const label = (id: string) => g.nodes.find((n) => n.id === id)!.label
  return g.edges.map((e) => `${label(e.from)} -${e.kind}-> ${label(e.to)}`)
}

describe('decodeInput — TreeNode', () => {
  it('reads a null-padded level-order array the way the harness builds it', () => {
    expect(edges(decodeInput('TreeNode', [5, 2, 9, null, null, 7, 11]))).toEqual([
      '5 -left-> 2', '5 -right-> 9', '9 -left-> 7', '9 -right-> 11',
    ])
  })

  it('does not reserve child slots for a null (unlike a perfect-tree encoding)', () => {
    // If null reserved slots, 4 and 7 would hang off the missing children of
    // 6's null sibling instead of off 6 itself.
    expect(edges(decodeInput('TreeNode', [8, 3, 10, 1, 6, null, 14, null, null, 4, 7, 13]))).toEqual([
      '8 -left-> 3', '8 -right-> 10', '3 -left-> 1', '3 -right-> 6', '10 -right-> 14',
      '6 -left-> 4', '6 -right-> 7', '14 -left-> 13',
    ])
  })

  it('distinguishes a lone left child from a lone right child', () => {
    expect(edges(decodeInput('TreeNode', [1, 2]))).toEqual(['1 -left-> 2'])
    expect(edges(decodeInput('TreeNode', [1, null, 2]))).toEqual(['1 -right-> 2'])
  })

  it('tracks depth, which is what the renderer rows by', () => {
    const g = decodeInput('TreeNode', [1, 2, 3, 4])[0]
    expect(g.nodes.map((n) => `${n.label}@${n.depth}`)).toEqual(['1@0', '2@1', '3@1', '4@2'])
  })

  it('treats [] and [null] as no tree at all', () => {
    expect(decodeInput('TreeNode', [])[0].nodes).toEqual([])
    expect(decodeInput('TreeNode', [null])[0].nodes).toEqual([])
  })
})

describe('decodeInput — list shapes', () => {
  it('chains a flat array', () => {
    expect(edges(decodeInput('ListNode', [1, 2, 3]))).toEqual(['1 -next-> 2', '2 -next-> 3'])
  })

  it('points a cyclic list tail back at index pos', () => {
    expect(edges(decodeInput('CyclicListNode', [[3, 2, 0, -4], 1]))).toEqual([
      '3 -next-> 2', '2 -next-> 0', '0 -next-> -4', '-4 -next-> 2',
    ])
  })

  it('leaves pos = -1 acyclic', () => {
    expect(edges(decodeInput('CyclicListNode', [[1, 2], -1]))).toEqual(['1 -next-> 2'])
  })

  it('adds random edges only where the index is non-null', () => {
    const g = decodeInput('RandomListNode', [[7, null], [13, 0], [11, 4], [10, 2], [1, 0]])
    expect(edges(g).filter((e) => e.includes('random'))).toEqual([
      '13 -random-> 7', '11 -random-> 1', '10 -random-> 11', '1 -random-> 7',
    ])
  })

  it('splits a List[ListNode] into one structure per sub-array', () => {
    const gs = decodeInput('List[ListNode]', [[1, 4, 5], [2, 6]])
    expect(gs.map((g) => g.nodes.map((n) => n.label))).toEqual([['1', '4', '5'], ['2', '6']])
  })
})

describe('decodeInput — GraphNode', () => {
  it('labels nodes by 1-based value and emits each undirected edge once', () => {
    const g = decodeInput('GraphNode', [[2, 4], [1, 3], [2, 4], [1, 3]])
    expect(g[0].nodes.map((n) => n.label)).toEqual(['1', '2', '3', '4'])
    expect(edges(g)).toEqual(['1 -neighbor-> 2', '1 -neighbor-> 4', '2 -neighbor-> 3', '3 -neighbor-> 4'])
  })

  it('reads [[]] as one isolated node and [] as no graph', () => {
    expect(decodeInput('GraphNode', [[]])[0].nodes).toHaveLength(1)
    expect(decodeInput('GraphNode', [])[0].nodes).toHaveLength(0)
  })
})

describe('decodeExpected', () => {
  it('decodes a returned tree the same way as an input one', () => {
    expect(decodeExpected('TreeNode', [1, 2])[0].nodes.map((n) => n.label)).toEqual(['1', '2'])
  })

  it('refuses CyclicListNode, whose output codec encodes an index, not a list', () => {
    // harness.py's `_encode_cyclic_node` returns the cycle-entry index (1 here),
    // which decodeInput would misread as a two-element [values, pos] pair.
    expect(decodeExpected('CyclicListNode', 1)).toEqual([])
  })

  it('decodes List[ListNode], whose output codec mirrors its input one', () => {
    // harness.py encodes a returned list of lists as one flat array per list.
    const graphs = decodeExpected('List[ListNode]', [[1, 2], [3]])
    expect(graphs.map((g) => g.nodes.map((n) => n.label))).toEqual([['1', '2'], ['3']])
  })

  it('ignores a non-node return type', () => {
    expect(decodeExpected('', [[3], [9, 20]])).toEqual([])
  })
})

describe('nodeParams', () => {
  const params = [{ name: 'root', type: 'TreeNode' }, { name: 'k', type: 'int' }]

  it('keeps the param index, since it indexes the test case input array', () => {
    expect(nodeParams([{ name: 'k', type: 'int' }, ...params], 'function')).toEqual([
      { index: 1, name: 'root', type: 'TreeNode' },
    ])
  })

  it('returns nothing for an operations-kind problem', () => {
    // Its input is [ops, args] (see format.ts), so param index 0 addresses the
    // op-name array, not `root` — a tree iterator would otherwise draw garbage.
    expect(nodeParams(params, 'operations')).toEqual([])
  })

  it('recognizes exactly the types harness.py has codecs for', () => {
    expect(isNodeType('TreeNode')).toBe(true)
    expect(isNodeType('List[int]')).toBe(false)
  })
})
