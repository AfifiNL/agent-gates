"""One summary of a repository: main, specs, lanes, the merge queue and the decision log.

`collect` gathers the data (also served read-only over MCP); `render` turns it into Markdown.
The output is deterministic: no timestamps, no absolute paths, everything sorted, and short
SHAs of a fixed length, so two runs on the same repository state print the same text.
"""

from __future__ import annotations

from typing import Any

from agent_gates import decisions, specs
from agent_gates import queue as queue_mod
from agent_gates.config import Config, ConfigError
from agent_gates.gate import check
from agent_gates.git import GitError, heads
from agent_gates.lanes import list_lanes
from agent_gates.repo import Repo

SHORT = 12
HISTORY_SHOWN = 5


def _short(sha: str | None) -> str:
    return sha[:SHORT] if sha else "-"


def collect(repo: Repo) -> dict[str, Any]:
    data: dict[str, Any] = {"main": {"branch": repo.main}}
    try:
        main_sha = repo.git.resolve(heads(repo.main))
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

    try:
        work = repo.working_config()
    except ConfigError:
        work = Config()  # unreadable working-tree config: fall back to the default folders
    try:
        spec_rows = specs.check_dir(repo.root, work.specs_dir)
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
            {"path": work.specs_dir, "title": "", "criteria": 0, "ticked": 0, "errors": [str(exc)]}
        ]

    if cfg is not None:
        data["lanes"] = [
            {
                "name": r["name"],
                "head": r.get("head"),
                "ahead": r.get("ahead"),
                "behind": r.get("behind"),
                "status": r["status"],
                "gate": r.get("gate"),
            }
            for r in list_lanes(repo.git, repo.state(), cfg)
        ]
    else:
        data["lanes"] = []

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
        logs = decisions.load(repo.root, work.decisions_dir)
        data["decisions"] = {
            "entries": [
                {"number": d.number, "title": d.title, "status": d.status}
                for d in sorted(logs, key=lambda d: (d.number, d.title))
            ],
            "errors": decisions.check(repo.root, work.decisions_dir),
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

    out += ["", f"## Specs ({len(data['specs'])})", ""]
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
    out += ["", f"## Decisions ({len(d['entries'])})", ""]
    if d["entries"]:
        out += ["| # | title | status |", "|---|---|---|"]
        for e in d["entries"]:
            out.append(f"| {e['number']:04d} | {_cell(e['title'])} | {e['status']} |")
    else:
        out.append("No decisions.")
    out += ["", "check: ok" if not d["errors"] else f"check: {len(d['errors'])} error(s)"]
    out += [f"- {_cell(e)}" for e in d["errors"]]
    return "\n".join(out) + "\n"
