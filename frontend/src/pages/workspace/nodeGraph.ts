/**
 * Decoders that turn a test case's wire JSON into a small graph the workspace
 * can *draw*.
 *
 * A `TreeNode`/`ListNode`/`GraphNode` test case is stored — and shown — as the
 * flat array the judge decodes (`[8,4,12,null,6,10]`), because that array
 * is the contract with `judge/harness.py`. It is also close to unreadable: you
 * have to replay the null-padded level-order rule in your head before you
 * know what tree you're looking at. These decoders replay it for you, mirroring
 * `harness.py`'s codecs exactly (`_build_tree`, `_build_list`,
 * `_build_cyclic_list`, `_build_random_list`, `_build_graph`) so the picture
 * can't disagree with what the judge actually built.
 *
 * The output is a generic `VizGraph` (nodes + typed edges) rather than a nested
 * `{children: []}` tree on purpose: half of these shapes — a cycle, a random
 * pointer, an adjacency list — are *not* trees and cannot be expressed as one.
 * See ADR-0003.
 */
import type { Kind, ParamSpec } from '../../api/types'

/** Param/return types `judge/harness.py`'s `_CODECS` builds node structures for. */
export const NODE_TYPES = [
  'TreeNode', 'ListNode', 'List[TreeNode]', 'List[ListNode]',
  'CyclicListNode', 'RandomListNode', 'GraphNode',
] as const
export type NodeType = (typeof NODE_TYPES)[number]

export function isNodeType(type: string): type is NodeType {
  return (NODE_TYPES as readonly string[]).includes(type)
}

/** `left`/`right` carry the binary-tree side; the rest name the pointer field. */
export type EdgeKind = 'left' | 'right' | 'next' | 'random' | 'neighbor'

export type VizNode = { id: string; label: string; depth: number }
export type VizEdge = { from: string; to: string; kind: EdgeKind }
/** One drawable structure. A `List[...]`-typed value decodes to several. */
export type VizGraph = {
  shape: 'tree' | 'list' | 'graph'
  nodes: VizNode[]
  edges: VizEdge[]
  /** Entry node (tree root / list head / graph entry); `null` when empty. */
  root: string | null
}

const empty = (shape: VizGraph['shape']): VizGraph => ({ shape, nodes: [], edges: [], root: null })

/** Null-padded level-order array -> tree (mirrors `_build_tree`). */
function decodeTree(values: unknown, ns = 'n'): VizGraph {
  const vals = Array.isArray(values) ? values : []
  if (vals.length === 0 || vals[0] === null || vals[0] === undefined) return empty('tree')
  const nodes: VizNode[] = [{ id: `${ns}0`, label: String(vals[0]), depth: 0 }]
  const depthOf = new Map([[`${ns}0`, 0]])
  const edges: VizEdge[] = []
  const queue = [`${ns}0`]
  let qi = 0
  let i = 1
  while (qi < queue.length && i < vals.length) {
    const parent = queue[qi++]
    for (const side of ['left', 'right'] as const) {
      if (i >= vals.length) break
      const v = vals[i++]
      // A null marks "no node here" and — unlike a perfect-tree encoding —
      // reserves no slots for its own children, so it is simply skipped.
      if (v === null || v === undefined) continue
      const id = `${ns}${i - 1}`
      const depth = depthOf.get(parent)! + 1
      depthOf.set(id, depth)
      nodes.push({ id, label: String(v), depth })
      edges.push({ from: parent, to: id, kind: side })
      queue.push(id)
    }
  }
  return { shape: 'tree', nodes, edges, root: `${ns}0` }
}

/** Flat array -> singly linked chain (mirrors `_build_list`). */
function decodeList(values: unknown, ns = 'n'): VizGraph {
  const vals = Array.isArray(values) ? values : []
  const nodes = vals.map((v, i) => ({ id: `${ns}${i}`, label: String(v), depth: 0 }))
  const edges = nodes.slice(1).map((n, i) => ({ from: nodes[i].id, to: n.id, kind: 'next' as const }))
  return { shape: 'list', nodes, edges, root: nodes[0]?.id ?? null }
}

/**
 * `[values, pos]` -> a chain whose tail points back at index `pos` (`-1`: no
 * cycle) — mirrors `_build_cyclic_list`. **Input only**: the matching *output*
 * codec (`_encode_cyclic_node`) emits a bare index, not this pair, which is why
 * `decodeExpected` refuses this type.
 */
function decodeCyclicList(payload: unknown): VizGraph {
  const [values, pos] = (Array.isArray(payload) ? payload : [[], -1]) as [unknown, unknown]
  const g = decodeList(values)
  if (typeof pos === 'number' && pos >= 0 && pos < g.nodes.length) {
    g.edges.push({ from: g.nodes[g.nodes.length - 1].id, to: g.nodes[pos].id, kind: 'next' })
  }
  return g
}

/** `[[val, randomIdx|null], ...]` -> chain plus `.random` edges (`_build_random_list`). */
function decodeRandomList(payload: unknown): VizGraph {
  const pairs = (Array.isArray(payload) ? payload : []) as unknown[]
  const g = decodeList(pairs.map((p) => (Array.isArray(p) ? p[0] : p)))
  pairs.forEach((p, i) => {
    const target = Array.isArray(p) ? p[1] : null
    if (typeof target === 'number' && target >= 0 && target < g.nodes.length) {
      g.edges.push({ from: g.nodes[i].id, to: g.nodes[target].id, kind: 'random' })
    }
  })
  return g
}

/**
 * Adjacency list -> undirected graph (`_build_graph`). Row `i` holds the
 * neighbor *values* of the node valued `i + 1` — 1-indexed by value, not by
 * position — so an edge is emitted once, from the lower index.
 */
function decodeGraph(values: unknown): VizGraph {
  const adj = Array.isArray(values) ? values : []
  const nodes = adj.map((_, i) => ({ id: `n${i}`, label: String(i + 1), depth: 0 }))
  const edges: VizEdge[] = []
  adj.forEach((neighbors, i) => {
    for (const v of Array.isArray(neighbors) ? neighbors : []) {
      const j = Number(v) - 1
      if (j >= 0 && j < nodes.length && i < j) {
        edges.push({ from: nodes[i].id, to: nodes[j].id, kind: 'neighbor' })
      }
    }
  })
  return { shape: 'graph', nodes, edges, root: nodes[0]?.id ?? null }
}

/** Lift a single-structure decoder over a `List[...]` value (`_build_each`). */
function decodeEach(one: (v: unknown, ns: string) => VizGraph, values: unknown): VizGraph[] {
  return (Array.isArray(values) ? values : []).map((v, i) => one(v, `l${i}n`))
}

/**
 * A test case's *input* value for a node-typed param -> the structures to draw
 * (more than one only for the `List[...]` types).
 */
export function decodeInput(type: NodeType, value: unknown): VizGraph[] {
  switch (type) {
    case 'TreeNode': return [decodeTree(value)]
    case 'ListNode': return [decodeList(value)]
    case 'List[TreeNode]': return decodeEach(decodeTree, value)
    case 'List[ListNode]': return decodeEach(decodeList, value)
    case 'CyclicListNode': return [decodeCyclicList(value)]
    case 'RandomListNode': return [decodeRandomList(value)]
    case 'GraphNode': return [decodeGraph(value)]
  }
}

/**
 * The same for a case's `expected`, keyed on the problem's `return_type`.
 *
 * Not simply `decodeInput`: two codecs in `harness.py` are deliberately
 * asymmetric, because the *answer* to those problems is an identity, not a
 * shape. `CyclicListNode` encodes a returned node as its index in the input
 * (`_encode_cyclic_node`) and `List[ListNode]` has no output codec at all, so
 * neither expected value is a structure — they get no diagram rather than a
 * wrong one.
 */
export function decodeExpected(returnType: string, value: unknown): VizGraph[] {
  if (returnType === 'CyclicListNode' || returnType === 'List[ListNode]') return []
  return isNodeType(returnType) ? decodeInput(returnType, value) : []
}

/**
 * The node-typed params of a problem, paired with their position in a test
 * case's `input` array.
 *
 * Gated on `kind === 'function'`: an operations-kind problem's input is
 * `[ops, args]` (see `format.ts`'s `isOperationsInput`), so param *index* `i`
 * indexes nothing in it — `input[i]` would be an op-name array. An operations
 * problem that declares a `TreeNode`/`ListNode` constructor param (a tree
 * iterator, say) would otherwise draw garbage.
 */
export function nodeParams(params: ParamSpec[], kind: Kind): { index: number; name: string; type: NodeType }[] {
  if (kind !== 'function') return []
  return params.flatMap((p, index) => (isNodeType(p.type) ? [{ index, name: p.name, type: p.type }] : []))
}
