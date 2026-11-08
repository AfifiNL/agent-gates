# Design notes

## Goals, in priority order

1. The main branch only ever points at a commit that passed the gate, as that exact commit.
2. A change cannot make its own gate weaker.
3. Agents (and people) can see what is expected (spec), what was decided (decision log) and
   where a change stands (lane, gate, queue) without asking.
4. No server, no database, no new service: plain git plus files.

## Decisions

**Gate records are keyed by the full commit SHA.** A record says "this tree, with this history,
passed these steps". Anything else (a branch name, a short SHA, a pull request number) can point
at different content tomorrow. A rebase or an amend that changes nothing visible still produces
a new SHA and needs a new run; that is the point.

**The gate runs on a clean, detached worktree of the commit.** Running in the developer's or the
agent's working copy would let uncommitted edits, untracked files or build output make a commit
pass that would fail on its own. A temporary worktree costs one checkout and is removed
afterwards (also on failure).

**The queue gates the merge, not the lane.** A lane can pass on its own and still break main
when combined with what merged before it. The queue builds the merge commit of main + lane, gates
that SHA, and fast-forwards main to it. Because main moves only by fast-forward to a gated
commit, every commit main ever points to has a record.

**The gate configuration comes from main.** If the gate read `.agent-gates.toml` from the commit
under test, a lane could replace the test step with `true` and pass. Reading it from main means
a configuration change takes effect only after it has itself been merged through the old gate.
The name of the main branch comes from the command line, not from a file. Records store the
SHA-256 of the configuration they ran under; `gate verify` rejects a record from another
configuration.

**Compare-and-swap on main.** Gating takes time, and someone may push in between. The queue
updates main only if it still points at the commit the candidate was built on (`git update-ref
<ref> <new> <old>`, or a fast-forward-only merge in the worktree that has main checked out).
Otherwise the lane goes back to the front and is rebuilt; after three races in one run the run
stops rather than spin.

**Fail closed everywhere.** No steps configured: fail. Timeout: fail. Program not found: fail
(exit 127). Steps run as argv lists without a shell. Refs that start with `-` or contain spaces
or control characters are refused before they reach git, so a ref cannot become a git option.

**State in `.git/agent-gates/`, not in the tree.** Gate records, lanes and the queue are local
facts about this clone. Putting them in the tree would make every gate run a commit, and a
commit cannot contain a record about itself. The shared `.git` directory is visible to every
worktree. Writes are atomic (temp file + rename) and serialised with a lock; the queue runner
holds its own lock so two runs cannot interleave.

**The MCP server is read-mostly.** Agents can read specs, gate status, the queue, lanes and
decisions, and propose a decision (written as `proposed`; a person accepts it). They cannot run
the gate, add to the queue or move branches through MCP: those are actions with side effects
that a person or a pipeline triggers. The repository is fixed at server start and no tool takes
a path.

**Specs and decisions are checked for structure only.** A checklist of acceptance criteria can
be ticked off and reviewed; prose cannot. Whether the criteria are good is a human judgement.
Decision numbers run without gaps and are never reused, and accepted records are superseded
rather than edited, so links to them stay valid.

**The CI template gates the exact commit too.** On `pull_request` it checks out the head SHA
(not GitHub's synthetic merge ref) and on `merge_group` the queue's candidate, and writes the SHA
it tested to the job summary. Third-party code runs only from SHA-pinned actions or from a
release binary whose SHA-256 is checked. Permissions default to read-only.

## Known gaps

- Gate records are not signed. Anyone with write access to `.git` can forge one; on a shared
  repository, CI (the template) is the authority and this tool is the local mirror of it.
- One candidate at a time. Batching or speculative parallel candidates would cut queue time for
  busy repositories.
- No clean-up of merged lanes yet.
