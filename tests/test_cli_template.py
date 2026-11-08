from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agent_gates.cli import main
from agent_gates.repo import Repo
from conftest import ROOT, commit

TEMPLATE = ROOT / "templates" / "github" / "gate.yml"
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def run(repo: Repo, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(["-C", str(repo.root), *argv])
    return code, capsys.readouterr().out


def test_cli_end_to_end(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(repo, capsys, "init")
    assert code == 0
    assert "docs/specs/001-example.md" in out
    assert ".agent-gates.toml" not in out  # never overwrites the existing config
    assert run(repo, capsys, "spec", "check")[0] == 0
    assert run(repo, capsys, "decision", "check")[0] == 0
    assert run(repo, capsys, "decision", "new", "Pin actions by SHA")[0] == 0
    assert "0002  proposed" in run(repo, capsys, "decision", "list")[1]
    code, out = run(repo, capsys, "gate", "verify", "main")
    assert code == 1
    assert "no gate record" in out
    code, out = run(repo, capsys, "gate", "run", "main")
    assert code == 0
    assert "gate PASSED" in out
    assert run(repo, capsys, "gate", "verify", "main")[0] == 0
    assert run(repo, capsys, "gate", "status", "main")[0] == 0
    assert run(repo, capsys, "lane", "new", "fix")[0] == 0
    assert "fix" in run(repo, capsys, "lane", "list")[1]
    assert run(repo, capsys, "queue", "add", "fix")[0] == 0
    code, out = run(repo, capsys, "queue", "run")
    assert code == 0
    assert "skipped" in out
    assert run(repo, capsys, "queue", "status")[0] == 0


def test_cli_failures_exit_nonzero(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    commit(repo.root, {"calc.py": "def add(a, b):\n    return 0\n"})
    code, out = run(repo, capsys, "gate", "run")
    assert code == 1
    assert "gate FAILED" in out
    assert main(["-C", str(repo.root), "queue", "add", "ghost"]) == 2
    assert main(["-C", str(repo.root), "--main", "trunk", "gate", "run"]) == 2
    assert run(repo, capsys, "spec", "check")[0] == 1  # no specs at all is an error


def test_not_a_repository(tmp_path: Path) -> None:
    assert main(["-C", str(tmp_path), "spec", "check"]) == 2


def test_demo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"multiply\s+merged", out)
    assert re.search(r"broken\s+rejected\s+gate failed", out)
    assert re.search(r"weaken-gate\s+rejected\s+gate failed", out)
    assert ": gate passed" in out


def test_workflow_template_is_pinned_and_least_privilege() -> None:
    text = TEMPLATE.read_text()
    wf = yaml.safe_load(text)
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert set(on) == {"pull_request", "merge_group"}
    assert "pull_request_target" not in text
    assert wf["permissions"] == {"contents": "read"}
    uses = [step["uses"] for job in wf["jobs"].values() for step in job["steps"] if "uses" in step]
    assert uses
    for u in uses:
        assert PINNED.match(u), f"not pinned by full commit SHA: {u}"
    for line in text.splitlines():
        if "uses:" in line:
            assert re.search(r"#\s*v\d+\.\d+\.\d+", line), f"no version comment: {line.strip()}"
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
        extra = job.get("permissions", {})
        assert set(extra) <= {"contents", "security-events"}
        assert extra.get("contents", "read") == "read"
    sha = wf["jobs"]["secret-scan"]["env"]["GITLEAKS_SHA256"]
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    assert "sha256sum -c" in text
