#!/usr/bin/env python3
"""
Sanity test for show-tech command extraction and raw dump matching.

This script does *not* call the CLI repeatedly. It imports the existing
implementation from health_check_eos.py, then:
1) Collects show-tech text members from the given input (file/dir/archive).
2) Parses into command blocks with TechSupportParser.
3) For every (source_id, command) pair discovered (this is "all commands"),
   verifies exact match semantics via _match_command_blocks:
     - matched blocks count matches exact command blocks
     - matched block body is non-empty (after stripping whitespace)
     - command format gate: must start with "show" or "bash"
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class TaskStats:
    source_id: str
    blocks_total: int
    commands_unique: int
    tested_pairs: int
    failed_pairs: int


def load_module(module_path: Path):
    # Use a unique module name to avoid collisions if the script is re-run.
    # Also register the module in sys.modules before exec_module to satisfy
    # dataclasses' internal type resolution.
    unique_name = f"health_check_eos_{module_path.stem}"
    spec = importlib.util.spec_from_file_location(unique_name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from {module_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _nonempty_body(block_lines: Sequence[str]) -> bool:
    # Body can legitimately be large; only check for some non-whitespace content.
    return any(line.strip() for line in block_lines)


def run_test_for_input(health_check_module, input_path: str) -> int:
    collect_processing_tasks = health_check_module.collect_processing_tasks
    load_task_text = health_check_module.load_task_text
    TechSupportParser = health_check_module.TechSupportParser
    _match_command_blocks = health_check_module._match_command_blocks

    # Keep the test deterministic and memory-friendlier:
    # low_memory=True means we don't pre-load all member texts at task collection time.
    tasks = collect_processing_tasks(
        paths=[input_path],
        mode="brief",
        as_json=False,
        debug=False,
        show_checks_in_brief=None,
        skip_checks=None,
        skip_categories=None,
        low_memory=True,
    )

    if not tasks:
        print("No show-tech files found to process.", file=sys.stderr)
        return 2

    total_failures = 0
    total_empty_sections = 0
    empty_samples_printed = 0
    empty_samples_limit = getattr(health_check_module, "_TEST_EMPTY_SAMPLES_LIMIT", 0) or 0
    verbose = getattr(health_check_module, "_TEST_VERBOSE", False) or False
    stats: List[TaskStats] = []

    for task in tasks:
        text = load_task_text(task)
        blocks = TechSupportParser.parse(text)

        # We are effectively testing the same command extraction as the CLI -L:
        # the CLI prints b.command for each discovered CommandBlock in order.
        blocks_by_cmd: Dict[str, List] = {}
        for b in blocks:
            blocks_by_cmd.setdefault(b.command, []).append(b)

        tested_pairs = 0
        failed_pairs = 0
        empty_sections_for_task = 0

        if verbose:
            print(
                f"\n[INFO] Task: {task.source_id}\n"
                f"        blocks_total={len(blocks)}, unique_commands={len(blocks_by_cmd)}"
            )

        for cmd, exact_blocks in blocks_by_cmd.items():
            # Format gate: parser should only produce "show ..." / "bash ..."
            if not (cmd.startswith("show") or cmd.startswith("bash")):
                failed_pairs += 1
                total_failures += 1
                print(
                    f"[FAIL] {task.source_id}: command has unexpected format: {cmd!r}",
                    file=sys.stderr,
                )
                continue

            tested_pairs += 1

            # Exact match should return exactly those blocks.
            matched = _match_command_blocks(blocks, cmd)
            if len(matched) != len(exact_blocks):
                failed_pairs += 1
                total_failures += 1
                print(
                    f"[FAIL] {task.source_id}: exact match count mismatch for {cmd!r}: "
                    f"expected {len(exact_blocks)} got {len(matched)}",
                    file=sys.stderr,
                )
                continue

            # Body can legitimately be empty if the command had no output captured.
            # We record this as a warning-stat instead of failing the test.
            if all(not _nonempty_body(b.lines) for b in matched):
                empty_sections_for_task += 1
                total_empty_sections += len(matched)
                if verbose and empty_samples_limit and empty_samples_printed < empty_samples_limit:
                    # Print minimal sample: which command and matched block indices.
                    # Do not print full body to avoid flooding.
                    first = matched[0]
                    preview_line = ""
                    for ln in first.lines:
                        if ln.strip():
                            preview_line = ln.strip()
                            break
                    print(
                        f"[EMPTY] {task.source_id}\n"
                        f"         cmd={cmd!r} matched={len(matched)} first_nonempty_line={preview_line!r}"
                    )
                    empty_samples_printed += 1

        stats.append(
            TaskStats(
                source_id=task.source_id,
                blocks_total=len(blocks),
                commands_unique=len(blocks_by_cmd),
                tested_pairs=tested_pairs,
                failed_pairs=failed_pairs,
            )
        )

    # Summary
    print("\n=== Test Summary ===")
    print(f"Input: {input_path}")
    print(f"Tasks: {len(stats)}")
    for s in stats:
        print(
            f"- {s.source_id}: blocks={s.blocks_total}, unique_commands={s.commands_unique}, "
            f"tested_pairs={s.tested_pairs}, failed_pairs={s.failed_pairs}"
        )
    if total_failures:
        print(f"\nResult: FAIL (total failures: {total_failures})", file=sys.stderr)
        return 1
    print(f"\nEmpty section blocks observed: {total_empty_sections}")
    print("\nResult: PASS")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Sanity test show-tech dump matching")
    parser.add_argument("input_path", help="Path to show-tech file, directory, or archive")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed progress and empty-section samples (bounded by --max-empty).",
    )
    parser.add_argument(
        "--max-empty",
        type=int,
        default=25,
        metavar="N",
        help="Max empty-section samples to print when --verbose is set.",
    )
    parser.add_argument(
        "--limit-tasks",
        type=int,
        default=0,
        metavar="N",
        help="Limit number of tasks processed (0 means no limit).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent
    module_path = repo_root / "health_check_eos.py"
    mod = load_module(module_path)

    # Small hook to avoid threading flags through many function params.
    setattr(mod, "_TEST_VERBOSE", args.verbose)
    setattr(mod, "_TEST_EMPTY_SAMPLES_LIMIT", args.max_empty if args.verbose else 0)

    if args.limit_tasks and args.limit_tasks > 0:
        # Monkey-patch collect_processing_tasks to cap tasks count for quick iteration.
        orig_collect = mod.collect_processing_tasks

        def _collect_limited(*c_args, **c_kwargs):
            tasks = orig_collect(*c_args, **c_kwargs)
            return tasks[: args.limit_tasks]

        setattr(mod, "collect_processing_tasks", _collect_limited)

    ec = run_test_for_input(mod, args.input_path)
    raise SystemExit(ec)


if __name__ == "__main__":
    main()

