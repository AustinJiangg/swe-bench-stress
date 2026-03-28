"""
Semantic comparison of unified diff patches.

Two patches are considered semantically equivalent when they apply the same
set of changes (additions and removals) to the same files, regardless of:
- Hunk line numbers (context may shift between runs)
- Amount of surrounding context lines
- Trailing whitespace on individual lines
- Ordering of independent hunks or files

This is used to verify that replaying an agent trajectory in an E2B sandbox
produces the same code changes as the original agent execution.
"""

from __future__ import annotations

import re


def normalize_patch(patch_text: str) -> frozenset[tuple[str, str, str]]:
    """
    Parse a unified diff into a canonical set of (filepath, removed, added) tuples.

    Each tuple represents one hunk's net changes with context stripped and
    trailing whitespace normalised on each line.

    Returns a frozenset so comparison is order-independent.
    """
    if not patch_text or not patch_text.strip():
        return frozenset()

    changes: list[tuple[str, str, str]] = []

    # Split into per-file sections
    file_sections = re.split(r"^diff --git ", patch_text, flags=re.MULTILINE)

    for section in file_sections:
        if not section.strip():
            continue

        # Extract filepath from --- a/path or +++ b/path
        filepath = ""
        for line in section.splitlines():
            if line.startswith("+++ b/"):
                filepath = line[6:].strip()
                break
            if line.startswith("--- a/"):
                filepath = line[6:].strip()

        if not filepath:
            # Try extracting from the diff --git header itself
            m = re.match(r"a/(.+?)\s+b/", section)
            if m:
                filepath = m.group(1)

        # Split into hunks
        hunks = re.split(r"^@@[^@]*@@.*$", section, flags=re.MULTILINE)

        for hunk in hunks:
            removed_lines: list[str] = []
            added_lines: list[str] = []

            for line in hunk.splitlines():
                if line.startswith("-") and not line.startswith("--- "):
                    removed_lines.append(line[1:].rstrip())
                elif line.startswith("+") and not line.startswith("+++ "):
                    added_lines.append(line[1:].rstrip())
                # Context lines (space prefix) and headers are ignored

            removed = "\n".join(removed_lines)
            added = "\n".join(added_lines)

            if removed or added:
                changes.append((filepath, removed, added))

    return frozenset(changes)


def patches_match(expected: str, actual: str) -> bool:
    """
    Return True if two unified diff patches are semantically equivalent.

    Both patches are normalised to strip line numbers, context, and trailing
    whitespace, then compared as unordered sets of (file, removals, additions).
    """
    # Both empty → match
    if not expected.strip() and not actual.strip():
        return True
    # One empty, one not → mismatch
    if not expected.strip() or not actual.strip():
        return False

    return normalize_patch(expected) == normalize_patch(actual)
