"""agent-gates: specs, lanes, a gate on the exact commit, a merge queue and a decision log."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from agent_gates import decisions, specs
from agent_gates import queue as queue_mod
from agent_gates.config import ConfigError
from agent_gates.gate import records, run_gate, verify
from agent_gates.git import GitError
from agent_gates.lanes import LaneError, list_lanes, new_lane
from agent_gates.repo import Repo, init


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def cmd_init(repo: Repo, _: argparse.Namespace) -> int:
    for path in init(repo.root, dt.date.today().isoformat()):
        print(f"wrote {path.relative_to(repo.root)}")
    print("next: commit these files on the main branch (the gate reads its configuration from")
    print("there), then run `agent-gates gate run main` once so the queue starts from a gated main")
    return 0


def cmd_spec_check(repo: Repo, _: argparse.Namespace) -> int:
    results = specs.check_dir(repo.root, repo.working_config().specs_dir)
    errors = [e for r in results for e in r.errors]
    for e in errors:
        print(e)
    criteria = sum(r.criteria for r in results)
    done = sum(r.done for r in results)
    print(
        f"{len(results)} spec(s), {criteria} acceptance criteria ({done} ticked), "
        f"{len(errors)} error(s)"
    )
    return 1 if errors or not results else 0


def cmd_lane_new(repo: Repo, args: argparse.Namespace) -> int:
    entry = new_lane(
        repo.git, repo.state(), repo.config(), args.name, Path(args.path) if args.path else None
    )
    print(
        f"lane {entry['name']}: branch {entry['branch']} at {entry['base_sha'][:12]}, "
        f"worktree {entry['path']}"
    )
    return 0


def cmd_lane_list(repo: Repo, _: argparse.Namespace) -> int:
    for row in list_lanes(repo.git, repo.state(), repo.config()):
        head = (row.get("head") or "-")[:12]
        print(
            f"{row['name']:<20} {row['status']:<14} head {head}  ahead {row.get('ahead', '-')}"
            f"  gate {row.get('gate') or '-'}"
        )
    return 0


def cmd_gate_run(repo: Repo, args: argparse.Namespace) -> int:
    sha = repo.git.resolve_user(args.ref)
    record = run_gate(
        repo.git,
        repo.state(),
        repo.config(),
        sha,
        config_from=repo.git.resolve(f"refs/heads/{repo.main}"),
    )
    for s in record.steps:
        verdict = "pass" if s.passed else ("timeout" if s.timed_out else f"fail ({s.exit_code})")
        print(f"  {s.name:<18} {verdict:<10} {s.duration_s:.2f}s")
        if not s.passed and s.output_tail:
            print("    " + s.output_tail.strip().replace("\n", "\n    "))
    print(f"gate {'PASSED' if record.passed else 'FAILED'} on {record.sha}")
    return 0 if record.passed else 1


def cmd_gate_status(repo: Repo, args: argparse.Namespace) -> int:
    sha = repo.git.resolve_user(args.ref)
    found = records(repo.git, repo.state(), sha)
    if not found:
        print(f"no gate record for {sha}")
        return 1
    _print_json([r.to_json() for r in found])
    return 0


def cmd_gate_verify(repo: Repo, args: argparse.Namespace) -> int:
    ok, message = verify(repo.git, repo.state(), repo.config(), repo.git.resolve_user(args.ref))
    print(message)
    return 0 if ok else 1


def cmd_queue_add(repo: Repo, args: argparse.Namespace) -> int:
    print("queue: " + ", ".join(queue_mod.add(repo.git, repo.state(), args.lane)))
    return 0


def cmd_queue_status(repo: Repo, _: argparse.Namespace) -> int:
    _print_json(queue_mod.status(repo.state()))
    return 0


def cmd_queue_run(repo: Repo, args: argparse.Namespace) -> int:
    outcomes = queue_mod.run(
        repo.git, repo.state(), repo.main, allow_config_change=frozenset(args.allow_config_change)
    )
    if not outcomes:
        print("queue is empty")
    for o in outcomes:
        cand = f" (candidate {o.candidate[:12]})" if o.candidate else ""
        print(f"  {o.lane:<20} {o.result:<9} {o.reason}{cand}")
    print(f"{repo.main} is at {repo.git.resolve(f'refs/heads/{repo.main}')[:12]}")
    return 1 if any(o.result == "rejected" for o in outcomes) else 0


def cmd_decision_new(repo: Repo, args: argparse.Namespace) -> int:
    path = decisions.new(repo.root, repo.working_config().decisions_dir, args.title)
    print(f"wrote {path.relative_to(repo.root.resolve())} (status: proposed)")
    return 0


def cmd_decision_check(repo: Repo, _: argparse.Namespace) -> int:
    cfg = repo.working_config()
    errors = decisions.check(repo.root, cfg.decisions_dir)
    for e in errors:
        print(e)
    print(
        f"{len(decisions.load(repo.root, cfg.decisions_dir))} decision(s), {len(errors)} error(s)"
    )
    return 1 if errors else 0


def cmd_decision_list(repo: Repo, _: argparse.Namespace) -> int:
    for d in decisions.load(repo.root, repo.working_config().decisions_dir):
        print(f"{d.number:04d}  {d.status:<11} {d.title}")
    return 0


def cmd_serve(repo: Repo, _: argparse.Namespace) -> int:  # pragma: no cover - stdio loop
    from agent_gates.mcp_server import build_server

    build_server(repo).run()
    return 0


def cmd_demo(_: Repo | None, __: argparse.Namespace) -> int:
    from agent_gates.demo import run_demo

    return run_demo()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-gates", description=__doc__)
    p.add_argument("-C", dest="repo", default=".", help="repository path (default: .)")
    p.add_argument("--main", default="main", help="the protected branch (default: main)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "write a starter config, example spec and first decision")
    add("serve", cmd_serve, "run the MCP server on stdio")
    add("demo", cmd_demo, "run the offline demo in a temporary repository")

    def group(name: str, help_: str) -> Any:
        return sub.add_parser(name, help=help_).add_subparsers(dest="sub", required=True)

    def leaf(g: Any, name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        sp: argparse.ArgumentParser = g.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    g = group("spec", "spec files")
    leaf(g, "check", cmd_spec_check, "check every spec's format")
    g = group("lane", "one branch + worktree per unit of work")
    leaf(g, "new", cmd_lane_new, "create a lane from main").add_argument("name")
    g.choices["new"].add_argument("--path", default=None)
    leaf(g, "list", cmd_lane_list, "list lanes")
    g = group("gate", "the gate on an exact commit")
    leaf(g, "run", cmd_gate_run, "run the gate on a commit").add_argument(
        "ref", nargs="?", default="HEAD"
    )
    leaf(g, "status", cmd_gate_status, "show a commit's gate record").add_argument("ref")
    leaf(g, "verify", cmd_gate_verify, "exit 0 only if this exact commit passed").add_argument(
        "ref"
    )
    g = group("queue", "the merge queue")
    leaf(g, "add", cmd_queue_add, "queue a lane").add_argument("lane")
    leaf(g, "status", cmd_queue_status, "show the queue and recent outcomes")
    leaf(
        g, "run", cmd_queue_run, "merge queued lanes whose candidate passes the gate"
    ).add_argument(
        "--allow-config-change",
        action="append",
        default=[],
        metavar="LANE",
        help="let this lane change the gate configuration (it must pass under old and new)",
    )
    g = group("decision", "the decision log")
    leaf(g, "new", cmd_decision_new, "add a proposed decision").add_argument("title")
    leaf(g, "check", cmd_decision_check, "check numbering and statuses")
    leaf(g, "list", cmd_decision_list, "list decisions")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = None if args.fn is cmd_demo else Repo.open(Path(args.repo), args.main)
        code: int = args.fn(repo, args)
    except (GitError, ConfigError, LaneError, decisions.DecisionError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
