/**
 * Draws a `VizGraph` (see `nodeGraph.ts`) as a small inline SVG.
 *
 * Hand-rolled rather than pulled from a library — the shapes this has to render
 * (a cycle, a random pointer, an adjacency list) are not trees, which is what
 * the off-the-shelf React tree renderers accept, and a general diagram engine
 * costs more than the whole rest of the app. ADR-0003 has the measurements.
 *
 * The drawing is intentionally static: across every sample case in
 * `seed/problems/*.json` the largest structure is 15 nodes and the longest label
 * is 7 characters, so it always fits without pan, zoom, or collapsing.
 */
import { useId } from 'react'
import type { VizEdge, VizGraph } from './nodeGraph'

const FONT = 12
const CHAR_W = 7.3      // ui-monospace at 12px, near enough for box sizing
const NODE_H = 26
const PAD_X = 9
const GAP = 22          // min horizontal air between two node boxes
const LEVEL_H = 54      // tree row pitch
const MARGIN = 6

type Pt = { x: number; y: number }

const widthOf = (label: string) => Math.max(NODE_H, label.length * CHAR_W + 2 * PAD_X)

/**
 * Tree layout: one slot per leaf, each parent centered between its children.
 *
 * The half-slot lean in the single-child cases is the point of doing this by
 * hand. A parent drawn directly above its only child is ambiguous — `[1,null,2]`
 * and `[1,2]` render identically — and that ambiguity is exactly what the
 * problem is usually about. Leaning the parent half a slot the other way makes
 * the edge visibly slant left or right. Each lean also reserves the half slot it
 * moves into, which keeps any two nodes at least one slot apart.
 */
function layoutTree(g: VizGraph, slot: number): Map<string, Pt> {
  const kids = new Map<string, { left?: string; right?: string }>()
  for (const e of g.edges) {
    const entry = kids.get(e.from) ?? {}
    if (e.kind === 'left') entry.left = e.to
    else entry.right = e.to
    kids.set(e.from, entry)
  }
  const depth = new Map(g.nodes.map((n) => [n.id, n.depth]))
  const at = new Map<string, Pt>()
  let cursor = 0
  const place = (id: string): number => {
    const { left, right } = kids.get(id) ?? {}
    let x: number
    if (left !== undefined && right !== undefined) {
      x = (place(left) + place(right)) / 2
    } else if (left !== undefined) {
      const cl = place(left)
      cursor += slot / 2
      x = cl + slot / 2
    } else if (right !== undefined) {
      cursor += slot / 2
      x = place(right) - slot / 2
    } else {
      x = cursor + slot / 2
      cursor += slot
    }
    at.set(id, { x, y: (depth.get(id) ?? 0) * LEVEL_H })
    return x
  }
  if (g.root) place(g.root)
  return at
}

/** Graph layout: nodes on a circle. Sample graphs are small (a handful of nodes),
 * where a ring reads clearly and needs no force simulation. */
function layoutCircle(g: VizGraph, slot: number): Map<string, Pt> {
  const n = g.nodes.length
  const radius = Math.max(slot, (n * slot) / (2 * Math.PI))
  return new Map(
    g.nodes.map((node, i) => {
      const a = (2 * Math.PI * i) / n - Math.PI / 2
      return [node.id, { x: radius * Math.cos(a), y: radius * Math.sin(a) }]
    }),
  )
}

/** Lists run left to right in input order — the order the values were written. */
function layoutRow(g: VizGraph, slot: number): Map<string, Pt> {
  return new Map(g.nodes.map((n, i) => [n.id, { x: i * slot, y: 0 }]))
}

/**
 * A pointer that doesn't go to the next box on the row — a cycle's back edge, or
 * a `.random` — arcs clear of the chain instead of running through it: cycles
 * below (they close the list), randoms above (they hop over it). The lift grows
 * with the span so two arcs over the same row don't trace the same curve.
 */
const liftFor = (span: number) => 22 + span * 0.2

function arcPath(a: Pt, b: Pt, up: boolean): string {
  const dir = up ? -1 : 1
  const edge = dir * (NODE_H / 2)
  const lift = dir * liftFor(Math.abs(b.x - a.x))
  return `M ${a.x} ${a.y + edge} Q ${(a.x + b.x) / 2} ${a.y + lift} ${b.x} ${b.y + edge}`
}

export function NodeDiagram({ graph, title }: { graph: VizGraph; title: string }) {
  const uid = useId().replace(/:/g, '')
  const arrow = `arrow-${uid}`
  if (graph.nodes.length === 0) return null

  const w = new Map(graph.nodes.map((n) => [n.id, widthOf(n.label)]))
  const slot = Math.max(...w.values()) + GAP
  const at =
    graph.shape === 'tree' ? layoutTree(graph, slot)
    : graph.shape === 'graph' ? layoutCircle(graph, slot)
    : layoutRow(graph, slot)

  // Arcs bulge outside the node boxes, so the viewBox has to account for them
  // rather than just the node extents — otherwise they get clipped. A quadratic
  // Bézier reaches only half its control offset, so that (not the full lift) is
  // the height to reserve; reserving the lift leaves a band of dead space.
  const arcs = graph.edges.filter((e) => isArc(e, at))
  const bulge = arcs.length
    ? NODE_H / 2 + Math.max(...arcs.map((e) => liftFor(Math.abs(at.get(e.to)!.x - at.get(e.from)!.x)))) / 2
    : 0
  const up = arcs.some((e) => e.kind === 'random')
  const down = arcs.some((e) => e.kind === 'next')
  const xs = [...at.values()].map((p) => p.x)
  const ys = [...at.values()].map((p) => p.y)
  const halfW = Math.max(...w.values()) / 2
  const minX = Math.min(...xs) - halfW - MARGIN
  const minY = Math.min(...ys) - NODE_H / 2 - MARGIN - (up ? bulge : 0)
  const width = Math.max(...xs) + halfW + MARGIN - minX
  const height = Math.max(...ys) + NODE_H / 2 + MARGIN + (down ? bulge : 0) - minY

  return (
    <svg
      viewBox={`${minX} ${minY} ${width} ${height}`}
      width={width}
      height={height}
      style={{ maxWidth: '100%', height: 'auto' }}
      role="img"
      aria-label={title}
    >
      <title>{title}</title>
      <defs>
        <marker id={arrow} viewBox="0 0 8 8" refX="6.5" refY="4" markerWidth="5" markerHeight="5" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" className="fill-zinc-500" />
        </marker>
      </defs>
      {/* Edges first: tree and graph edges run under the opaque node boxes, so
          they need no trimming to the box outline. */}
      <g className="stroke-zinc-500" fill="none">
        {graph.edges.map((e, i) => {
          const a = at.get(e.from)!
          const b = at.get(e.to)!
          if (isArc(e, at)) {
            return (
              <path
                key={i}
                d={arcPath(a, b, e.kind === 'random')}
                strokeDasharray={e.kind === 'random' ? '4 3' : undefined}
                markerEnd={`url(#${arrow})`}
              />
            )
          }
          if (e.kind === 'next') {
            // Same row, so trim horizontally to the two box edges.
            return (
              <line
                key={i}
                x1={a.x + w.get(e.from)! / 2 + 2}
                y1={a.y}
                x2={b.x - w.get(e.to)! / 2 - 6}
                y2={b.y}
                markerEnd={`url(#${arrow})`}
              />
            )
          }
          return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
        })}
      </g>
      {graph.nodes.map((n) => {
        const p = at.get(n.id)!
        const nw = w.get(n.id)!
        return (
          <g key={n.id}>
            <rect
              x={p.x - nw / 2}
              y={p.y - NODE_H / 2}
              width={nw}
              height={NODE_H}
              rx={NODE_H / 2}
              className="fill-zinc-800 stroke-zinc-600"
            />
            <text
              x={p.x}
              y={p.y}
              textAnchor="middle"
              dominantBaseline="central"
              fontSize={FONT}
              fontFamily="ui-monospace, monospace"
              className="fill-zinc-100"
            >
              {n.label}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

/** A `next` that points backwards (a cycle) or any `.random` needs an arc. */
function isArc(e: VizEdge, at: Map<string, Pt>): boolean {
  return e.kind === 'random' || (e.kind === 'next' && at.get(e.to)!.x <= at.get(e.from)!.x)
}
