"""Offline demo in a throwaway repository: three lanes, one merge queue run.

* `multiply` adds a feature with a test            -> merged
* `broken`   changes behaviour and breaks a test     -> rejected by the gate
* `weaken-gate` breaks a test AND edits the gate config to skip the tests -> still rejected,
  because the gate reads its configuration from main, not from the lane
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_gates.cli import main as cli

CALC = "def add(a, b):\n    return a + b\n"
TESTS = """\
import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
"""
SPEC = """\
# Multiply two numbers

## Problem

The calculator can add but not multiply.

## Acceptance criteria

- [ ] `multiply(a, b)` returns the product.
- [ ] A unit test covers it.

## Out of scope

- Division.
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603,S607


def _commit(cwd: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (cwd / name).parent.mkdir(parents=True, exist_ok=True)
        (cwd / name).write_text(text, encoding="utf-8")
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)


def _step(title: str, *argv: str) -> int:
    print(f"\n== {title}\n$ agent-gates {' '.join(argv[2:])}")
    return cli(list(argv))


def run_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-gates-demo-") as tmp:
        repo = Path(tmp) / "shop"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.name", "Demo Developer")
        _git(repo, "config", "user.email", "dev@example.com")
        cli(["-C", str(repo), "init"])
        config = (repo / ".agent-gates.toml").read_text(encoding="utf-8")
        python = json.dumps([sys.executable, "-m", "unittest", "-q"])
        config = config.replace('run = "python3 -m unittest -q"', f"run = {python}")
        _commit(
            repo,
            {
                ".agent-gates.toml": config,
                "calc.py": CALC,
                "test_calc.py": TESTS,
                "docs/specs/002-multiply.md": SPEC,
            },
            "Initial commit",
        )
        c = ["-C", str(repo)]

        _step("specs are checked like code", *c, "spec", "check")
        _step("one lane per unit of work", *c, "lane", "new", "multiply")
        for name in ("broken", "weaken-gate"):
            cli([*c, "lane", "new", name])
        lanes = Path(tmp) / "shop.lanes"
        _commit(
            lanes / "multiply",
            {
                "mul.py": "def multiply(a, b):\n    return a * b\n",
                "test_multiply.py": "import unittest\n\nfrom mul import multiply\n\n\n"
                "class MulTest(unittest.TestCase):\n    def test_mul(self):\n"
                "        self.assertEqual(multiply(3, 4), 12)\n",
            },
            "Add multiply",
        )
        _commit(lanes / "broken", {"calc.py": "def add(a, b):\n    return a - b\n"}, "Refactor add")
        weak = config.replace(f"run = {python}", 'run = "true"')
        _commit(
            lanes / "weaken-gate",
            {
                "test_calc.py": TESTS.replace("5)", "6)"),
                ".agent-gates.toml": weak,
            },
            "Speed up the gate",
        )
        cli([*c, "lane", "list"])

        for name in ("multiply", "broken", "weaken-gate"):
            cli([*c, "queue", "add", name])
        _step("the merge queue gates each candidate merge commit", *c, "queue", "run")
        _step("main is at a commit that passed the gate", *c, "gate", "verify", "main")
        _step("decisions are numbered records", *c, "decision", "new", "Gate every merge")
        cli([*c, "decision", "check"])
    return 0
