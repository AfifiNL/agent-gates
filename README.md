# agent-gates

Guard rails for AI coding agents working on a git repository. Work starts from a written spec
with checkable acceptance criteria, each unit of work gets its own branch and worktree (a
"lane"), a gate runs the project's checks on one exact commit, and a merge queue moves the main
branch only to a commit that passed that gate. Decisions go into a numbered log.

- **CLI** (`agent-gates`): `spec check`, `lane new|list|done`, `gate run|status|verify`,
  `queue add|run|status`, `decision new|check|list`, `status`, `init`, `serve`, `demo`.
- **MCP server** (stdio) so an agent can read the specs, a commit's gate status, the queue and
  the decision log, and propose a decision. It cannot run the gate, queue a lane or move a branch.
- **GitHub workflow template** (`templates/github/gate.yml`) that runs the same checks on the
  exact commit under test, plus a secret scan, dependency review and CodeQL; every action is
  pinned by commit SHA.
- Python 3.12, standard library plus the MCP SDK. Gate state lives in `.git/agent-gates/`, so
  nothing extra is committed and every worktree sees the same records.

## What it does

`make demo` builds a throwaway repository, gates its first commit, opens three lanes and runs
the merge queue once. `multiply` adds a feature with a test; `broken` changes behaviour and
breaks a test; `weaken-gate` breaks a test and edits the gate configuration to skip the tests.
Output (abridged, one long line wrapped):

```text
== the merge queue gates each candidate merge commit
$ agent-gates queue run
  multiply             merged    gate passed on the candidate (candidate c4613740274a)
  broken               rejected  gate failed at step 'tests' (current configuration) (candidate 5d0d3df87fbf)
  weaken-gate          rejected  the lane changes .agent-gates.toml; an operator must allow it with
                                 `queue run --allow-config-change weaken-gate` (candidate 07ebfa60f2b3)
main is at c4613740274a

== main is at a commit that passed the gate
$ agent-gates gate verify main
c4613740274a: gate passed
```

The gate reads its configuration from main, never from the lane it judges, and a lane that
changes the configuration is refused unless an operator allows it on the command line.

## Quick start

Requires Python 3.12+, git 2.31+ and [uv](https://docs.astral.sh/uv/).

```bash
make install     # .venv with dev tools (locked)
make demo        # the run above, in a temporary repository
make check       # lint, strict types, 124 tests (what CI runs)
```

In a new repository (run as written in a scratch directory, with a git identity configured):

```bash
alias agent-gates="uv run --project /path/to/agent-gates agent-gates"
mkdir shop && cd shop && git init -q -b main
cat > test_smoke.py <<'EOF'
import unittest


class Smoke(unittest.TestCase):
    def test_truth(self):
        self.assertTrue(True)
EOF
agent-gates init                  # .agent-gates.toml, an example spec, the first decision
git add -A && git commit -qm "Start with agent-gates"
agent-gates gate run main         # gate the starting point; the queue builds on gated commits only
agent-gates lane new export-csv   # branch lane/export-csv + worktree ../shop.lanes/export-csv
cd ../shop.lanes/export-csv       # the agent works and commits here
echo "id,total" > export.csv && git add -A && git commit -qm "Add export header"
cd ../../shop
agent-gates queue add export-csv
agent-gates queue run             # gate the merge candidate; fast-forward main only on pass
agent-gates gate verify main      # exit 0 only if this exact commit passed
```

The starter gate runs `python3 -I -m unittest discover -q`, which fails when it finds no
tests, so the repository needs at least one (`test_smoke.py` above). Replace the step with your
own checks.

`.agent-gates.toml`:

```toml
main = "main"
specs_dir = "docs/specs"          # relative paths inside the repository only
decisions_dir = "docs/decisions"

[gate]
env = []                          # extra environment variables the steps may see

[[gate.steps]]
name = "tests"
run = "python3 -I -m unittest discover -q"   # split like a shell would, run without a shell
timeout_s = 600
```

A longer worked example (a spec, two lanes, one rejected by the gate, one merged, a decision
recorded, with the real output of one run) is in [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md).

`agent-gates status` prints one Markdown summary: main and its gate state, the specs and the
decision log as committed on main (with their check results), the lanes (ahead and behind main,
state, gate state of their head), and the merge queue with its last outcomes (`--json` for the
same data). It reads git and the shared state in `.git/agent-gates/`, not a working tree, so it
prints the same text from any worktree of the clone while nothing changes. It has no
timestamps, and the queue stores its reasons without machine-specific paths.

Finished lanes: `agent-gates lane done <name>` removes the worktree and the branch only if main
contains the lane, the worktree is still on the lane branch and has no uncommitted, untracked or
ignored files (removing a worktree deletes ignored files such as a local `.env` too), and the
lane is not queued. While a queue run is in progress it refuses instead of waiting. `--force` removes
the lane anyway and discards its work. `queue run --cleanup` applies the same rules to each lane
right after it merges and records "lane kept: <reason>" for any it cannot remove safely. Both
work from inside the lane's own folder.

MCP: `agent-gates -C /path/to/repo serve` speaks MCP on stdin/stdout. Tools: `repo_status`
(the `status` summary, as data and Markdown), `check_specs`, `gate_status`, `queue_status`,
`list_lanes`, `list_decisions`, `add_decision`.

## How it works

```text
spec (docs/specs/NNN-name.md)          acceptance criteria as a checklist
   │
lane new ──► branch lane/<name> + its own worktree, started from main
   │
   │  agent commits in the lane
   ▼
queue add ─► queue run, one lane at a time:
   0. main's tip must have a passing gate record                   not gated → run stops
   1. candidate = merge commit of main + lane (temporary worktree)  conflict → rejected
   2. lane changes .agent-gates.toml?                               not allowed → rejected
   3. gate on that exact candidate SHA, in a fresh detached worktree:
      built-in spec and decision-log checks, then the steps from
      main's .agent-gates.toml                                      fail → rejected
   4. fast-forward main to the candidate if main has not moved      moved → queued again
   ▼
main ──────► always a commit with a passing gate record
```

- **Exact commit.** A gate record is stored under the full 40-character SHA and the hash of the
  configuration it ran under. A record for one commit never counts for another, so a rebase, an
  amend or a merge needs its own run. `gate verify` accepts a record made under main's current
  configuration or under the configuration committed in that commit, nothing else.
- **Branches by full name.** Branches are resolved as `refs/heads/...`, so a tag called `main`
  or `lane/x` cannot stand in for the branch.
- **Clean checkout.** The gate runs in a temporary detached worktree of the commit, so
  uncommitted edits and untracked files in someone's working copy cannot make it pass.
- **Configuration from main.** The steps are read from the committed `.agent-gates.toml` on the
  main branch; the branch name comes from the command line (`--main`). A lane that changes the
  file is rejected unless the operator runs `queue run --allow-config-change <lane>` (not
  available over MCP). Even then the new configuration must parse, keep at least one step and
  the same main branch, and the candidate must pass under the old and the new configuration.
- **Built-in checks.** Every gate run also checks the commit's specs and decision log, so two
  lanes that each add decision 0002 cannot both merge.
- **Minimal step environment.** Steps get `PATH`, the locale variables, a temporary `HOME` and
  the variables listed in `[gate] env`; nothing else from the caller's environment. A step that
  moves any git ref (a branch, a tag) fails the gate.
- **Compare-and-swap.** main is advanced only if it still points where the candidate was built
  from; otherwise the lane stays first in the queue and is rebuilt on the new main. After three
  such races in one run, the run stops and leaves the lane queued. If main is checked out, that
  working tree must be clean (untracked files included) and is fast-forwarded.
- **Fail closed.** A configuration without steps never passes. A step that times out or cannot
  start fails. Steps run without a shell. Unexpected errors reject the lane with the error and
  take it out of the queue; problems on the operator's side (main not gated, main's working tree
  not clean) stop the run and keep the lane. A queue run holds a lock, so two runs cannot
  interleave. Candidate merge commits are made by the tool and are not signed.
- **Decisions** are files `NNNN-name.md` with `- Status: proposed|accepted|rejected|superseded|deprecated`;
  numbering must run from 0001 without gaps, and a superseded record must name an existing
  successor. `decision new` never overwrites a file.

## GitHub workflow template

`templates/github/gate.yml` is a template to copy into another repository's
`.github/workflows/`. It runs on `merge_group` (GitHub's merge queue), where it checks out the
queue's candidate commit, the exact commit main will move to, and on `pull_request`, where it
checks out the pull request's head commit for early feedback. It writes the gated SHA to the job
summary and runs `make check`, a gitleaks scan of the full repository history (release binary,
SHA-256 verified), dependency review and CodeQL. Permissions are `contents: read`, plus
`security-events: write` and `actions: read` for the CodeQL job. Checkouts do not keep
credentials.

Two things the template cannot do by itself. On `pull_request`, GitHub runs the workflow file
from the pull request, and the jobs run the pull request's Makefile and tests: a pull request can
change its own checks. Turn on the merge queue and make these jobs **required** through branch
protection or a ruleset (or an organisation-level required workflow), and review changes to the
workflow and the Makefile like any other code.

CodeQL and dependency review are free on public repositories; on private repositories they need
GitHub Advanced Security. The template is checked by the tests (YAML parses, every `uses:` is
pinned to a 40-character SHA with a version comment, least-privilege permissions), but it has
not been run on GitHub from this repository.

## Limits

- **Gate steps run untrusted code.** The steps execute the change under review: its tests, its
  build files, anything they import. The minimal environment and the ref check stop accidents
  and simple tricks, not a determined change. A step can still read files the operator's user
  can read, write into `.git` (including gate records), or fake its own result, for example a
  test that reports success without testing anything. The `-I` flag in the starter command stops
  a change from replacing Python's test runner, not from weakening its own tests. Run the queue
  in a disposable sandbox or CI runner, and rely on code review and CI on protected
  infrastructure as the authority.
- **Local and single-machine.** Lanes, the queue and gate records live in one clone's
  `.git/agent-gates/`. There is no server, no sharing between machines, and no signature on gate
  records: anyone who can write to `.git` can forge one.
- **The gate is as good as its steps.** It runs your commands; it does not know whether your
  tests are meaningful.
- **Queue runs are sequential.** One candidate is gated at a time; there is no batching or
  speculative parallel gating.
- **Spec and decision checks are about structure**, not content: they check that acceptance
  criteria exist and are a checklist, not that they are good.
- **Clean-up is opt-in.** Merged lanes keep their branch and worktree until `lane done` or
  `queue run --cleanup`; rejected lanes stay until someone looks at them.
- SHA-1 repositories only (git's default). Tested on Linux only (it uses `fcntl` file locks).

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Hossam Afifi.
