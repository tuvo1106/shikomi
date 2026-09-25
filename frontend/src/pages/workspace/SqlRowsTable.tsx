/**
 * A SQL result set — a list of rows, each row a list of cell values — rendered
 * as a lightweight table instead of a dense `JSON.stringify` array-of-arrays.
 * Matches how the problem statement itself shows example rows. Deliberately
 * headerless: judge/harness_sql.py returns only positional cell values, never
 * column names, so there's nothing to put in a header row.
 */
export function SqlRowsTable({ rows }: { rows: unknown[][] }) {
  if (rows.length === 0) return <div className="text-zinc-500">(empty result set)</div>
  return (
    <table className="w-full border-collapse text-xs">
      <tbody>
        {rows.map((row, i) => (
          <tr key={i} className="border-b border-zinc-800 last:border-0">
            {row.map((cell, j) => (
              <td key={j} className="py-0.5 pr-4 align-top">
                {cell === null ? <span className="italic text-zinc-600">NULL</span> : String(cell)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
