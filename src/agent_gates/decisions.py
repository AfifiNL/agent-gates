"""A numbered decision log: `<decisions_dir>/NNNN-short-name.md`, one decision per file.

Numbers are contiguous from 0001 and never reused. Each file starts with `# NNNN. Title` and a
`- Status: <status>` line. A superseded decision names its successor (`Superseded by: NNNN`),
which must exist. Records are not edited after acceptance; a new decision supersedes them.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_gates.config import inside, safe_relative_dir

FILENAME = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
TITLE = re.compile(r"^# (\d{4})\. (\S.*)$")
STATUS = re.compile(r"^- Status: (\w+)\s*$", re.MULTILINE)
SUPERSEDED_BY = re.compile(r"^- Superseded by: (\d{4})\s*$", re.MULTILINE)
STATUSES = ("proposed", "accepted", "rejected", "superseded", "deprecated")

TEMPLATE = """\
# {number}. {title}

- Status: proposed
- Date: {date}

## Context

{context}

## Decision

{decision}

## Consequences

{consequences}
"""


class DecisionError(ValueError):
    pass


@dataclass
class Decision:
    path: str
    number: int
    title: str
    status: str
    superseded_by: int | None = None
    errors: list[str] = field(default_factory=list)


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60].rstrip("-") or "decision"


def parse(path: Path, rel: str) -> Decision:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = FILENAME.match(path.name)
    first = text.splitlines()[0] if text else ""
    t = TITLE.match(first)
    s = STATUS.search(text)
    sb = SUPERSEDED_BY.search(text)
    d = Decision(
        rel,
        int(m.group(1)) if m else 0,
        t.group(2) if t else "",
        s.group(1).lower() if s else "",
        int(sb.group(1)) if sb else None,
    )
    if not m:
        d.errors.append(f"{rel}: file name must look like 0001-short-name.md")
    if not t:
        d.errors.append(f"{rel}:1: first line must be '# NNNN. Title'")
    elif m and t.group(1) != m.group(1):
        d.errors.append(f"{rel}:1: heading number {t.group(1)} != file number {m.group(1)}")
    if d.status not in STATUSES:
        d.errors.append(f"{rel}: status must be one of {', '.join(STATUSES)}")
    if d.status == "superseded" and d.superseded_by is None:
        d.errors.append(f"{rel}: superseded, but no '- Superseded by: NNNN' line")
    return d


def load(root: Path, decisions_dir: str) -> list[Decision]:
    rel = safe_relative_dir(decisions_dir, "decisions_dir")
    base = inside(root, rel)
    if not base.is_dir():
        return []
    return [
        parse(inside(root, f"{rel}/{p.name}"), f"{rel}/{p.name}")
        for p in sorted(base.glob("*.md"))
        if p.name.lower() != "readme.md"
    ]


def check(root: Path, decisions_dir: str) -> list[str]:
    decisions = load(root, decisions_dir)
    errors = [e for d in decisions for e in d.errors]
    numbers = sorted(d.number for d in decisions if d.number)
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        dupes = sorted({n for n in numbers if numbers.count(n) > 1})
        missing = sorted(set(expected) - set(numbers))
        if dupes:
            errors.append(f"duplicate decision numbers: {', '.join(f'{n:04d}' for n in dupes)}")
        if missing or not dupes:
            errors.append(
                f"decision numbers must run 0001..{len(numbers):04d} without gaps; "
                f"found {', '.join(f'{n:04d}' for n in numbers)}"
            )
    known = set(numbers)
    for d in decisions:
        if d.superseded_by is not None and d.superseded_by not in known:
            errors.append(f"{d.path}: superseded by {d.superseded_by:04d}, which does not exist")
        if d.superseded_by is not None and d.superseded_by == d.number:
            errors.append(f"{d.path}: cannot supersede itself")
    return errors


def new(
    root: Path,
    decisions_dir: str,
    title: str,
    context: str = "TODO: what forces this decision?",
    decision: str = "TODO: what we decided.",
    consequences: str = "TODO: what becomes easier or harder.",
    today: dt.date | None = None,
) -> Path:
    title = " ".join(title.split())
    if not title:
        raise DecisionError("a decision needs a title")
    if len(title) > 120:
        raise DecisionError("keep the title under 120 characters")
    base = inside(root, safe_relative_dir(decisions_dir, "decisions_dir"))
    base.mkdir(parents=True, exist_ok=True)
    existing = [d.number for d in load(root, decisions_dir) if d.number]
    number = f"{max(existing, default=0) + 1:04d}"
    path = base / f"{number}-{slugify(title)}.md"
    with path.open("x", encoding="utf-8") as fh:  # never overwrite an existing record
        fh.write(
            TEMPLATE.format(
                number=number,
                title=title,
                date=(today or dt.date.today()).isoformat(),
                context=context.strip(),
                decision=decision.strip(),
                consequences=consequences.strip(),
            )
        )
    return path
