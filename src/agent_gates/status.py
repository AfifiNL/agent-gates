"""One summary of a repository: main, specs, lanes, the merge queue and the decision log.

`collect` gathers the data (also served read-only over MCP); `render` turns it into Markdown.
Specs and decisions are read from main's commit, lanes, the queue and gate records from the
shared state in `.git/agent-gates/`, so the output does not depend on which worktree it is run
from. No timestamps, rows sorted, SHAs shortened to a fixed length. Stored queue reasons are kept
free of machine-specific paths when they are written.
"""

from __future__ import annotations

from typing import Any

from agent_gates import decisions, specs
from agent_gates import queue as queue_mod
from agent_gates.config import Config, ConfigError, safe_relative_dir
from agent_gates.gate import check
from agent_gates.git import GitError, heads
from agent_gates.lanes import list_lanes
from agent_gates.repo import Repo

SHORT = 12
HISTORY_SHOWN = 5


def _short(sha: str | None) -> str:
    return sha[:SHORT] if sha else "-"


def _committed_markdown(repo: Repo, commit: str, rel_dir: str) -> list[tuple[str, str]]:
    """(path, text) of the Markdown files in `rel_dir` as committed in `commit`. Read from git,
    not from any working tree, so every worktree sees the same thing."""
    rel = safe_relative_dir(rel_dir, "directory")
    out = repo.git.run("ls-tree", "--name-only", "-z", commit, f"{rel}/")
    files = []
    for path in sorted(filter(None, out.split("\0"))):
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".md") and name.lower() != "readme.md":
            files.append((path, repo.git.show(commit, path) or ""))
    return files


def collect(repo: Repo) -> dict[str, Any]:
    """Status data. Specs and decisions are read from main's commit (not a working tree);
    lanes, the queue and gate records from the shared state."""
    data: dict[str, Any] = {"main": {"branch": repo.main}}
    try:
        main_sha: str | None = repo.git.resolve(heads(repo.main))
    except GitError:
        main_sha = None
    data["main"]["head"] = main_sha
    cfg = None
    try:
        cfg = repo.config()
        data["config"] = {"ok": True, "steps": [s.name for s in cfg.steps], "error": None}
    except ConfigError as exc:
        data["config"] = {"ok": False, "steps": [], "error": str(exc)}
    data["main"]["gate"] = "unknown"
    if cfg is not None and main_sha is not None:
        data["main"]["gate"] = check(repo.git, repo.state(), cfg, main_sha)[0]

    dirs = cfg or Config()  # main's folders, or the defaults when its config is unreadable
    try:
        spec_rows = specs.check_files(
            _committed_markdown(repo, main_sha, dirs.specs_dir) if main_sha else []
        )
        data["specs"] = [
            {
                "path": r.path,
                "title": r.title,
                "criteria": r.criteria,
                "ticked": r.done,
                "errors": r.errors,
            }
            for r in spec_rows
        ]
    except ConfigError as exc:
        data["specs"] = [
            {"path": dirs.specs_dir, "title": "", "criteria": 0, "ticked": 0, "errors": [str(exc)]}
        ]

    # Lanes need only the main branch name; their gate state needs the configuration.
    data["lanes"] = [
        {
            "name": r["name"],
            "head": r.get("head"),
            "ahead": r.get("ahead"),
            "behind": r.get("behind"),
            "status": r["status"],
            "gate": r.get("gate") if cfg is not None else "unknown",
        }
        for r in list_lanes(repo.git, repo.state(), repo.main, cfg)
    ]

    q = queue_mod.status(repo.state())
    data["queue"] = {
        "queued": list(q["queued"]),
        "recent": [
            {
                "lane": h["lane"],
                "result": h["result"],
                "reason": h["reason"],
                "candidate": h.get("candidate"),
            }
            for h in q["history"][-HISTORY_SHOWN:]
        ],
    }

    try:
        logs = [
            decisions.parse_text(path, text)
            for path, text in (
                _committed_markdown(repo, main_sha, dirs.decisions_dir) if main_sha else []
            )
        ]
        data["decisions"] = {
            "entries": [
                {"number": d.number, "title": d.title, "status": d.status}
                for d in sorted(logs, key=lambda d: (d.number, d.title))
            ],
            "errors": decisions.check_decisions(logs),
        }
    except ConfigError as exc:
        data["decisions"] = {"entries": [], "errors": [str(exc)]}
    return data


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(data: dict[str, Any]) -> str:
    m = data["main"]
    out = ["# agent-gates status", ""]
    out.append(f"main: `{m['branch']}` at `{_short(m['head'])}`, gate: {m['gate']}")
    cfg = data["config"]
    out.append(
        f"gate steps (from main): {', '.join(cfg['steps'])}"
        if cfg["ok"]
        else f"gate configuration: ERROR {cfg['error']}"
    )

    out += ["", f"## Specs on main ({len(data['specs'])})", ""]
    if data["specs"]:
        out += ["| spec | title | criteria | ticked | check |", "|---|---|---|---|---|"]
        for s in data["specs"]:
            verdict = "ok" if not s["errors"] else f"{len(s['errors'])} error(s)"
            out.append(
                f"| {_cell(s['path'])} | {_cell(s['title'])} | {s['criteria']} | "
                f"{s['ticked']} | {verdict} |"
            )
        errors = [e for s in data["specs"] for e in s["errors"]]
        if errors:
            out += ["", *[f"- {_cell(e)}" for e in errors]]
    else:
        out.append("No specs.")

    out += ["", f"## Lanes ({len(data['lanes'])})", ""]
    if data["lanes"]:
        out += [
            "| lane | head | ahead | behind | state | gate on head |",
            "|---|---|---|---|---|---|",
        ]
        for lane in data["lanes"]:
            ahead = "-" if lane["ahead"] is None else lane["ahead"]
            behind = "-" if lane["behind"] is None else lane["behind"]
            out.append(
                f"| {lane['name']} | `{_short(lane['head'])}` | {ahead} | {behind} | "
                f"{lane['status']} | {lane['gate'] or '-'} |"
            )
    else:
        out.append("No lanes.")

    q = data["queue"]
    out += ["", "## Merge queue", ""]
    out.append("queued: " + (", ".join(q["queued"]) if q["queued"] else "nothing"))
    if q["recent"]:
        out += [
            "",
            f"Last {len(q['recent'])} outcome(s), oldest first:",
            "",
            "| lane | result | candidate | reason |",
            "|---|---|---|---|",
        ]
        for h in q["recent"]:
            out.append(
                f"| {h['lane']} | {h['result']} | `{_short(h['candidate'])}` | "
                f"{_cell(h['reason'])} |"
            )

    d = data["decisions"]
    out += ["", f"## Decisions on main ({len(d['entries'])})", ""]
    if d["entries"]:
        out += ["| # | title | status |", "|---|---|---|"]
        for e in d["entries"]:
            out.append(f"| {e['number']:04d} | {_cell(e['title'])} | {e['status']} |")
    else:
        out.append("No decisions.")
    out += ["", "check: ok" if not d["errors"] else f"check: {len(d['errors'])} error(s)"]
    out += [f"- {_cell(e)}" for e in d["errors"]]
    return "\n".join(out) + "\n"
