"""Spec files: what an agent is asked to build, and how anyone can tell it is done.

A spec is `<specs_dir>/NNN-short-name.md` with a `# Title` and three sections:
`## Problem` (not empty), `## Acceptance criteria` (at least one checklist item `- [ ] ...`,
and only checklist items, so each criterion can be ticked off) and `## Out of scope`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FILENAME = re.compile(r"^(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
HEADING = re.compile(r"^##\s+(.+?)\s*$")
CHECK_ITEM = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+\S")
BULLET = re.compile(r"^\s*[-*+]\s+|^\s*\d+[.)]\s+")
REQUIRED = ("problem", "acceptance criteria", "out of scope")


@dataclass
class SpecResult:
    path: str
    number: str | None
    title: str
    criteria: int = 0
    done: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_text(path: str, text: str) -> SpecResult:
    name = Path(path).name
    m = FILENAME.match(name)
    lines = text.splitlines()
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else ""
    res = SpecResult(path, m.group(1) if m else None, title)
    if not m:
        res.errors.append(f"{path}: file name must look like 001-short-name.md")
    if not title:
        res.errors.append(f"{path}:1: first line must be a '# Title'")
    sections: dict[str, tuple[int, list[str]]] = {}
    current: str | None = None
    for n, line in enumerate(lines, 1):
        h = HEADING.match(line)
        if h:
            current = h.group(1).lower()
            if current in sections:
                res.errors.append(f"{path}:{n}: duplicate section '## {h.group(1)}'")
            sections[current] = (n, [])
        elif current is not None:
            sections[current][1].append(line)
    for req in REQUIRED:
        if req not in sections:
            res.errors.append(f"{path}: missing section '## {req.capitalize()}'")
    if "problem" in sections and not any(s.strip() for s in sections["problem"][1]):
        res.errors.append(f"{path}:{sections['problem'][0]}: '## Problem' is empty")
    if "acceptance criteria" in sections:
        start, body = sections["acceptance criteria"]
        for offset, line in enumerate(body, 1):
            if CHECK_ITEM.match(line):
                res.criteria += 1
                res.done += "[ ]" not in line
            elif BULLET.match(line):
                res.errors.append(
                    f"{path}:{start + offset}: acceptance criteria must be checklist items "
                    "('- [ ] ...')"
                )
        if res.criteria == 0:
            res.errors.append(f"{path}:{start}: '## Acceptance criteria' has no checklist item")
    return res


def check_dir(root: Path, specs_dir: str) -> list[SpecResult]:
    base = root / specs_dir
    if not base.is_dir():
        return []
    results = [
        check_text(str(p.relative_to(root)), p.read_text(encoding="utf-8"))
        for p in sorted(base.glob("*.md"))
        if p.name.lower() != "readme.md"
    ]
    seen: dict[str, str] = {}
    for r in results:
        if r.number is None:
            continue
        if r.number in seen:
            r.errors.append(f"{r.path}: number {r.number} is also used by {seen[r.number]}")
        else:
            seen[r.number] = r.path
    return results
