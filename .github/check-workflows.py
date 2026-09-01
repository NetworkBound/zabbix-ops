#!/usr/bin/env python3
"""Reject duplicate mapping keys in workflow files.

PyYAML, and most editors, accept a duplicate key silently and keep the last
occurrence. GitHub rejects the whole workflow instead, and the result is a run
with no jobs, no logs and nothing to read -- so the local check passes and the
push fails, which is the worst order for the two to happen in.

This is deliberately not a full YAML parser. It only needs to answer one
question: does any mapping in this file set the same key twice? A line-based
check answers that for workflow files, which are plain block-style YAML, and it
does so with no dependency -- the same standard-library-only rule the tools in
this repository follow.

Usage: check-workflows.py [path ...]     (default: .github/workflows/*.yml)
Exit 1 if any duplicate is found.
"""
import glob
import re
import sys

# A key at the start of a mapping entry: indent, name, colon, then end-of-line
# or a value. Sequence entries ("- name: x") open a new mapping each time, so
# they are tracked separately per item.
KEY = re.compile(r'^(?P<indent> *)(?P<dash>- )?(?P<key>[A-Za-z_][\w.-]*)\s*:(?: |$)')


def check(path: str) -> list[str]:
    problems = []
    # indent -> {key: line number} for the mapping currently open at that depth.
    seen: dict[int, dict[str, int]] = {}
    in_block_scalar_at = None

    for lineno, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.rstrip("\n")
        stripped = line.strip()

        # Inside a block scalar (run: | ...) the content is not YAML.
        if in_block_scalar_at is not None:
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= in_block_scalar_at:
                in_block_scalar_at = None
            else:
                continue

        if not stripped or stripped.startswith("#"):
            continue

        m = KEY.match(line)
        if not m:
            continue

        indent = len(m.group("indent"))
        key = m.group("key")
        # "- name: x" starts a new mapping, so anything recorded at this depth
        # belonged to the previous list item.
        if m.group("dash"):
            seen.pop(indent + 2, None)
            indent += 2

        # Leaving a deeper level closes every mapping nested inside it.
        for deeper in [d for d in seen if d > indent]:
            del seen[deeper]

        bucket = seen.setdefault(indent, {})
        if key in bucket:
            problems.append(
                f"{path}:{lineno}: duplicate key '{key}' "
                f"(first set on line {bucket[key]})")
        else:
            bucket[key] = lineno

        if stripped.endswith((": |", ": >", ": |-", ": >-", ": |+", ": >+")):
            in_block_scalar_at = indent

    return problems


def main(argv: list[str]) -> int:
    paths = argv[1:] or sorted(glob.glob(".github/workflows/*.yml") +
                               glob.glob(".github/workflows/*.yaml"))
    if not paths:
        print("no workflow files found", file=sys.stderr)
        return 0
    bad = 0
    for p in paths:
        for problem in check(p):
            # The ::error prefix makes it land on the file in the GitHub UI.
            print(f"::error file={p}::{problem}")
            print(problem, file=sys.stderr)
            bad = 1
    if not bad:
        print(f"{len(paths)} workflow file(s), no duplicate keys")
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv))
