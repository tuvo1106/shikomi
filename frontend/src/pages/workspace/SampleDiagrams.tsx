/**
 * The diagrams under one worked example: every node-typed input the case
 * supplies, then the expected structure when the problem returns one.
 *
 * This sits beside the raw arrays rather than replacing them — the array is
 * still the thing you type into the editor and the thing the judge decodes, so
 * both are on screen (`Workspace.tsx`). Nothing renders for a problem with no
 * node-typed params, which is most of the catalog.
 */
import type { ProblemDetail, SampleCase } from '../../api/types'
import { NodeDiagram } from './NodeDiagram'
import { decodeExpected, decodeInput, nodeParams, type VizGraph } from './nodeGraph'

type Labeled = { label: string; graph: VizGraph; raw: unknown }

/** A `List[...]`-typed value draws several structures, so they get indexed
 * labels (`lists[0]`, `lists[1]`); a single structure keeps the bare name. */
function label(name: string, graphs: VizGraph[], raw: unknown): Labeled[] {
  return graphs.map((graph, i) => ({
    label: graphs.length > 1 ? `${name}[${i}]` : name,
    graph,
    raw: graphs.length > 1 && Array.isArray(raw) ? raw[i] : raw,
  }))
}

export function SampleDiagrams({ problem, sample }: { problem: ProblemDetail; sample: SampleCase }) {
  const diagrams: Labeled[] = [
    ...nodeParams(problem.params, problem.kind).flatMap((p) =>
      label(p.name, decodeInput(p.type, sample.input[p.index]), sample.input[p.index]),
    ),
    ...label('output', decodeExpected(problem.return_type, sample.expected), sample.expected),
  ].filter((d) => d.graph.nodes.length > 0)

  if (diagrams.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap items-start gap-x-6 gap-y-3 border-t border-zinc-800 pt-2">
      {diagrams.map((d) => (
        <div key={d.label}>
          <div className="mb-0.5 font-mono text-[10px] text-zinc-500">{d.label}</div>
          <NodeDiagram graph={d.graph} title={`${d.label} = ${JSON.stringify(d.raw)}`} />
        </div>
      ))}
    </div>
  )
}
