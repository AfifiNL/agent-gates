from __future__ import annotations

import sys
from typing import Any

import anyio
from mcp import Client, StdioServerParameters

from agent_gates import gate, lanes
from agent_gates.mcp_server import build_server
from agent_gates.repo import Repo
from conftest import commit

SPEC = """\
# Export

## Problem

No export.

## Acceptance criteria

- [ ] CSV export.

## Out of scope

- PDF.
"""


def _call(repo: Repo, tool: str, args: dict[str, Any]) -> Any:
    result: dict[str, Any] = {}

    async def go() -> None:
        async with Client(build_server(repo), mode="legacy") as client:  # type: ignore[arg-type]
            res = await client.call_tool(tool, args)
            assert not res.is_error, res
            result["value"] = res.structured_content

    anyio.run(go)
    return result["value"]


def test_tool_list(repo: Repo) -> None:
    async def go() -> list[str]:
        async with Client(build_server(repo)) as client:  # type: ignore[arg-type]
            return sorted(t.name for t in (await client.list_tools()).tools)

    assert anyio.run(go) == [
        "add_decision",
        "check_specs",
        "gate_status",
        "list_decisions",
        "list_lanes",
        "queue_status",
        "repo_status",
    ]


def test_check_specs(repo: Repo) -> None:
    commit(repo.root, {"docs/specs/001-export.md": SPEC, "docs/specs/002-bad.md": "nothing"})
    out = _call(repo, "check_specs", {})["result"]
    assert [(s["path"], s["criteria"], bool(s["errors"])) for s in out] == [
        ("docs/specs/001-export.md", 1, False),
        ("docs/specs/002-bad.md", 0, True),
    ]


def test_gate_status(repo: Repo) -> None:
    assert _call(repo, "gate_status", {"ref": "main"})["passed"] is False
    gate.run_gate(repo.git, repo.state(), repo.config(), "main")
    out = _call(repo, "gate_status", {"ref": "main"})
    assert out["passed"] is True
    assert len(out["sha"]) == 40
    bad = _call(repo, "gate_status", {"ref": "--upload-pack=x"})
    assert bad["passed"] is False
    assert "not a valid ref" in bad["message"]


def test_decisions_and_lanes_and_queue(repo: Repo) -> None:
    out = _call(
        repo,
        "add_decision",
        {"title": "Use a merge queue", "context": "c", "decision": "d", "consequences": "e"},
    )
    assert out == {"path": "docs/decisions/0001-use-a-merge-queue.md", "status": "proposed"}
    listed = _call(repo, "list_decisions", {})["result"]
    assert listed[0]["status"] == "proposed"
    lanes.new_lane(repo.git, repo.state(), repo.config(), "x")
    assert _call(repo, "list_lanes", {})["result"][0]["name"] == "x"
    assert _call(repo, "queue_status", {}) == {"queued": [], "history": []}
    assert "error" in _call(
        repo, "add_decision", {"title": " ", "context": "", "decision": "", "consequences": ""}
    )


def test_stdio_entry_point(repo: Repo) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agent_gates.cli", "-C", str(repo.root), "serve"]
    )

    async def go() -> Any:
        async with Client(params) as client:
            return (await client.call_tool("queue_status", {})).structured_content

    assert anyio.run(go) == {"queued": [], "history": []}
