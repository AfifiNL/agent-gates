"""MCP server (stdio) for coding agents: read the specs, the gate status of a commit, the merge
queue and the decision log, and propose a new decision.

The repository is fixed when the server starts. No tool takes a file path, runs the gate or
moves a branch: running commands and merging stay with the CLI and CI, where a person or a
pipeline decides when they happen.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from agent_gates import decisions, specs
from agent_gates import queue as queue_mod
from agent_gates.config import ConfigError
from agent_gates.gate import verify
from agent_gates.git import GitError
from agent_gates.lanes import list_lanes as _list_lanes
from agent_gates.repo import Repo

INSTRUCTIONS = (
    "Before coding, read the spec you were given (check_specs) and work only towards its "
    "acceptance criteria. A change reaches main only through the merge queue after the gate "
    "passes on the exact merged commit. Record any decision that changes how the code is built "
    "or run with add_decision; a person accepts it."
)


def build_server(repo: Repo) -> MCPServer:
    server = MCPServer("agent-gates", instructions=INSTRUCTIONS)

    @server.tool()
    def check_specs() -> list[dict[str, Any]]:
        """All specs with their acceptance-criteria counts and any format errors."""
        try:
            cfg = repo.working_config()
            return [
                {
                    "path": r.path,
                    "title": r.title,
                    "criteria": r.criteria,
                    "done": r.done,
                    "errors": r.errors,
                }
                for r in specs.check_dir(repo.root, cfg.specs_dir)
            ]
        except ConfigError as exc:
            return [{"error": str(exc)}]

    @server.tool()
    def gate_status(ref: str) -> dict[str, Any]:
        """Whether this exact commit (a SHA or a branch name) passed the gate under the
        current configuration on main."""
        try:
            sha = repo.git.resolve_user(ref)
            ok, message = verify(repo.git, repo.state(), repo.config(), sha)
        except (GitError, ConfigError) as exc:
            return {"ref": ref, "passed": False, "message": str(exc)}
        return {"ref": ref, "sha": sha, "passed": ok, "message": message}

    @server.tool()
    def queue_status() -> dict[str, Any]:
        """Lanes waiting in the merge queue, and the last 20 queue outcomes."""
        return queue_mod.status(repo.state())

    @server.tool()
    def list_lanes() -> list[dict[str, Any]]:
        """Lanes (one branch and worktree each), their head commit and gate result."""
        try:
            return _list_lanes(repo.git, repo.state(), repo.config())
        except ConfigError as exc:
            return [{"error": str(exc)}]

    @server.tool()
    def list_decisions() -> list[dict[str, Any]]:
        """The decision log: number, title, status."""
        try:
            cfg = repo.working_config()
            return [
                {"number": d.number, "title": d.title, "status": d.status, "path": d.path}
                for d in decisions.load(repo.root, cfg.decisions_dir)
            ]
        except ConfigError as exc:
            return [{"error": str(exc)}]

    @server.tool()
    def add_decision(title: str, context: str, decision: str, consequences: str) -> dict[str, str]:
        """Propose a decision. It is written with status 'proposed'; a person accepts it."""
        try:
            cfg = repo.working_config()
            path = decisions.new(
                repo.root, cfg.decisions_dir, title, context, decision, consequences
            )
        except (decisions.DecisionError, ConfigError, FileExistsError) as exc:
            return {"error": str(exc)}
        return {"path": str(path.relative_to(repo.root.resolve())), "status": "proposed"}

    return server
