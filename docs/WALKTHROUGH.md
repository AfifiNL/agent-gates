# Worked example

A small shop project goes from a written spec to a merged feature. Two agents work in two lanes
at the same time; the merge queue rejects one change because the gate fails, merges the other,
and the decision behind the feature is recorded through a lane as well.

This is the output of one real run of [`walkthrough.sh`](walkthrough.sh) in an empty scratch
directory on 2026-11-08. Lines starting with `$` are the commands; `# now in` shows the folder
the next commands run in (commits in a lane folder land on that lane's branch, not on main). Commit SHAs differ on every run. To repeat it:

```bash
mkdir /tmp/walk && cd /tmp/walk
AGENT_GATES="uv run --project /path/to/agent-gates agent-gates" /path/to/agent-gates/docs/walkthrough.sh .
```

What to look for:

- `faster-total` changes `total()` so that it drops the cents. The queue gates the merge
  candidate (main + lane), the existing test fails, and the lane is rejected; main does not move.
- `discount` is merged; `--cleanup` then removes its worktree and branch, because main now
  contains it.
- `lane done faster-total` refuses (the lane is not merged); `--force` discards it on purpose.
- The decision goes through a lane like any other change, so the built-in decision-log check runs
  on it before it reaches main.
- `status` ends with one summary: main gated, both specs well formed, no open lanes, the last
  queue outcomes, and the decision log.

```text
$ git init -q -b main shop
$ cd shop
# now in shop/
$ cat > pricing.py <<'EOF'   # a price total
def total(prices):
    return round(sum(prices), 2)
EOF
$ cat > test_pricing.py <<'EOF'   # its test
import unittest

from pricing import total


class TotalTest(unittest.TestCase):
    def test_total_keeps_cents(self):
        self.assertEqual(total([1.25, 2.50]), 3.75)
EOF
$ agent-gates init
wrote .agent-gates.toml
wrote docs/specs/001-example.md
wrote docs/decisions/0001-record-decisions-in-numbered-files.md
next: commit these files on the main branch (the gate reads its configuration from
there), then run `agent-gates gate run main` once so the queue starts from a gated main
$ cat > docs/specs/002-bulk-discount.md <<'EOF'   # what we want built
# Bulk discount

## Problem

Customers who buy many items pay the full price for each one.

## Acceptance criteria

- [ ] Orders of 10 or more items get 10% off the total.
- [ ] Orders of fewer than 10 items pay the full total.
- [ ] Totals keep their cents.

## Out of scope

- Discount codes.
EOF
$ git add -A
$ git commit -qm "Pricing module, gate configuration, bulk-discount spec"
$ agent-gates spec check
2 spec(s), 5 acceptance criteria (0 ticked), 0 error(s)
$ agent-gates gate run main
  builtin:specs      pass       0.00s
  builtin:decisions  pass       0.00s
  tests              pass       0.04s
gate PASSED on d538d917947cd1e1886c21a5183dea30001c065b

# Two agents, two lanes.
$ agent-gates lane new discount
lane discount: branch lane/discount at d538d917947c, worktree ../shop.lanes/discount
$ agent-gates lane new faster-total
lane faster-total: branch lane/faster-total at d538d917947c, worktree ../shop.lanes/faster-total
$ cd ../shop.lanes/discount
# now in shop.lanes/discount/
$ cat > discount.py <<'EOF'   # the feature, in its own module
from pricing import total


def order_total(prices):
    amount = total(prices)
    return round(amount * 0.9, 2) if len(prices) >= 10 else amount
EOF
$ cat > test_discount.py <<'EOF'   # tests for the acceptance criteria
import unittest

from discount import order_total


class DiscountTest(unittest.TestCase):
    def test_ten_items_get_ten_percent_off(self):
        self.assertEqual(order_total([10.0] * 10), 90.0)

    def test_nine_items_pay_full_price(self):
        self.assertEqual(order_total([10.0] * 9), 90.0)
EOF
$ git add -A
$ git commit -qm "Bulk discount from 10 items"
$ cd ../faster-total
# now in shop.lanes/faster-total/
$ cat > pricing.py <<'EOF'   # a 'speed-up' that drops the cents
def total(prices):
    return int(sum(prices))
EOF
$ git commit -qam "Faster total"
$ cd ../../shop
# now in shop/

# Both lanes go into the merge queue.
$ agent-gates queue add faster-total
queue: faster-total
$ agent-gates queue add discount
queue: faster-total, discount
$ agent-gates queue run --cleanup
  faster-total         rejected  gate failed at step 'tests' (current configuration) (candidate e13258948161)
  discount             merged    gate passed on the candidate (candidate f478dd48cc59)
                       cleanup   lane discount: removed worktree ../shop.lanes/discount, branch lane/discount (was 70b348a804bd)
main is at f478dd48cc59
(exit 1)
$ agent-gates lane done faster-total
error: lane 'faster-total' is not merged into main; use --force to discard it
(exit 2)
$ agent-gates lane done faster-total --force
lane faster-total: removed worktree ../shop.lanes/faster-total, branch lane/faster-total (was 84cf2623095e)

# The decision behind the feature goes through a lane too.
$ agent-gates lane new record-discount-decision
lane record-discount-decision: branch lane/record-discount-decision at f478dd48cc59, worktree ../shop.lanes/record-discount-decision
$ cd ../shop.lanes/record-discount-decision
# now in shop.lanes/record-discount-decision/
$ agent-gates decision new "Bulk discount starts at 10 items"
wrote docs/decisions/0002-bulk-discount-starts-at-10-items.md (status: proposed)
$ $EDITOR docs/decisions/0002-bulk-discount-starts-at-10-items.md   # accept it and fill in the three sections
$ cat docs/decisions/0002-bulk-discount-starts-at-10-items.md
# 0002. Bulk discount starts at 10 items

- Status: accepted
- Date: 2026-11-08

## Context

Large orders were paying full price.

## Decision

10% off the total from 10 items; no discount codes.

## Consequences

Simple to explain; tiers can come later.
$ git add -A
$ git commit -qm "Record the bulk-discount decision"
$ cd ../../shop
# now in shop/
$ agent-gates queue add record-discount-decision
queue: record-discount-decision
$ agent-gates queue run --cleanup
  record-discount-decision  merged    gate passed on the candidate (candidate 590bf180e76d)
                            cleanup   lane record-discount-decision: removed worktree ../shop.lanes/record-discount-decision, branch lane/record-discount-decision (was 4296cfde7ab7)
main is at 590bf180e76d
$ agent-gates gate verify main
590bf180e76d: gate passed
$ agent-gates decision check
2 decision(s), 0 error(s)

$ agent-gates status
# agent-gates status

main: `main` at `590bf180e76d`, gate: passed
gate steps (from main): tests

## Specs on main (2)

| spec | title | criteria | ticked | check |
|---|---|---|---|---|
| docs/specs/001-example.md | Example: what a spec looks like | 2 | 0 | ok |
| docs/specs/002-bulk-discount.md | Bulk discount | 3 | 0 | ok |

## Lanes (0)

No lanes.

## Merge queue

queued: nothing

Last 3 outcome(s), oldest first:

| lane | result | candidate | reason |
|---|---|---|---|
| faster-total | rejected | `e13258948161` | gate failed at step 'tests' (current configuration) |
| discount | merged | `f478dd48cc59` | gate passed on the candidate |
| record-discount-decision | merged | `590bf180e76d` | gate passed on the candidate |

## Decisions on main (2)

| # | title | status |
|---|---|---|
| 0001 | Record decisions in numbered files | accepted |
| 0002 | Bulk discount starts at 10 items | accepted |

check: ok
```
