#!/usr/bin/env python3
"""Write a `seed/problems/*.json` file in the repo's established compact style
(DESIGN.md §7.1; `.claude/skills/author-problem/`).

Plain `json.dumps(obj, indent=2)` breaks every element of every nested array
onto its own line, not just top-level fields — for a problem with a large
hidden test case (an array of thousands of ints), that turns a two-field
change into a multi-thousand-line diff. The established style instead keeps
the top-level document indented, but renders `params`/`constraints`/`tags`
and every `test_cases[]`/`solutions[]` entry as a single compact line
regardless of its size.

Usage:
    # As a library, when building a problem dict from scratch:
    from format_seed_problem import dump_problem
    pathlib.Path("seed/problems/pair-sum.json").write_text(dump_problem(problem))

    # As a CLI, to reformat a file already on disk in place:
    python scripts/format_seed_problem.py seed/problems/pair-sum.json
"""
import json
import sys

_COMPACT_LIST_KEYS = ("params", "constraints", "tags", "comparison")
_COMPACT_ITEM_KEYS = ("test_cases", "solutions")


def _compact(obj) -> str:
    return json.dumps(obj, separators=(", ", ": "))


def dump_problem(problem: dict) -> str:
    """Render a problem dict as the repo's compact seed JSON style.

    Round-trips to an identical object (`json.loads(dump_problem(p)) == p`) —
    this only changes whitespace, never values.
    """
    lines = ["{"]
    keys = list(problem.keys())
    for idx, key in enumerate(keys):
        comma = "," if idx < len(keys) - 1 else ""
        value = problem[key]
        if key in _COMPACT_LIST_KEYS:
            lines.append(f'  "{key}": {_compact(value)}{comma}')
        elif key in _COMPACT_ITEM_KEYS:
            lines.append(f'  "{key}": [')
            for i, item in enumerate(value):
                item_comma = "," if i < len(value) - 1 else ""
                lines.append(f"    {_compact(item)}{item_comma}")
            lines.append(f"  ]{comma}")
        else:
            # Reuse json.dumps for correct string/scalar escaping, then strip
            # the single-key object wrapper it produces down to just the pair.
            lines.append(f"  {_compact({key: value})[1:-1]}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: format_seed_problem.py <seed/problems/*.json> [...]", file=sys.stderr)
        return 2
    for path in sys.argv[1:]:
        with open(path) as f:
            problem = json.load(f)
        text = dump_problem(problem)
        assert json.loads(text) == problem, f"round-trip mismatch for {path}"
        with open(path, "w") as f:
            f.write(text)
        print(f"formatted {path} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
