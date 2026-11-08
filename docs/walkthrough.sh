#!/usr/bin/env bash
# Worked example: a spec, two lanes, one rejected by the gate, one merged, a decision recorded.
#
#   AGENT_GATES="uv run --project /path/to/agent-gates agent-gates" docs/walkthrough.sh <empty dir>
#
# Prints every command before it runs it; docs/WALKTHROUGH.md is the output of one real run.
set -euo pipefail
AG=${AGENT_GATES:-agent-gates}
# Print a command the way you would type it (double quotes only where needed).
say() {
  local out="" a
  for a in "$@"; do
    if [[ $a =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then out+=" $a"; else out+=" \"$a\""; fi
  done
  echo "\$${out}"
}
ag() { say agent-gates "$@"; $AG "$@"; }
ag_may_fail() { say agent-gates "$@"; set +e; $AG "$@"; echo "(exit $?)"; set -e; }
show() { say "$@"; "$@"; }
write() { echo "\$ cat > $1 <<'EOF'   # $2"; tee "$1"; echo "EOF"; }
go() { echo "\$ cd $1"; cd "$1"; echo "# now in ${PWD#"$ROOT/"}/"; }

cd "$1"
ROOT=$PWD
show git init -q -b main shop
go shop
git config user.name "Example Dev"
git config user.email dev@example.com

write pricing.py "a price total" <<'EOF'
def total(prices):
    return round(sum(prices), 2)
EOF
write test_pricing.py "its test" <<'EOF'
import unittest

from pricing import total


class TotalTest(unittest.TestCase):
    def test_total_keeps_cents(self):
        self.assertEqual(total([1.25, 2.50]), 3.75)
EOF
ag init
write docs/specs/002-bulk-discount.md "what we want built" <<'EOF'
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
show git add -A
show git commit -qm "Pricing module, gate configuration, bulk-discount spec"
ag spec check
ag gate run main

echo
echo "# Two agents, two lanes."
ag lane new discount
ag lane new faster-total

go ../shop.lanes/discount
write discount.py "the feature, in its own module" <<'EOF'
from pricing import total


def order_total(prices):
    amount = total(prices)
    return round(amount * 0.9, 2) if len(prices) >= 10 else amount
EOF
write test_discount.py "tests for the acceptance criteria" <<'EOF'
import unittest

from discount import order_total


class DiscountTest(unittest.TestCase):
    def test_ten_items_get_ten_percent_off(self):
        self.assertEqual(order_total([10.0] * 10), 90.0)

    def test_nine_items_pay_full_price(self):
        self.assertEqual(order_total([10.0] * 9), 90.0)
EOF
show git add -A
show git commit -qm "Bulk discount from 10 items"

go ../faster-total
write pricing.py "a 'speed-up' that drops the cents" <<'EOF'
def total(prices):
    return int(sum(prices))
EOF
show git commit -qam "Faster total"
go ../../shop

echo
echo "# Both lanes go into the merge queue."
ag queue add faster-total
ag queue add discount
ag_may_fail queue run --cleanup
ag_may_fail lane done faster-total
ag lane done faster-total --force

echo
echo "# The decision behind the feature goes through a lane too."
ag lane new record-discount-decision
go ../shop.lanes/record-discount-decision
ag decision new "Bulk discount starts at 10 items"
DECISION=docs/decisions/0002-bulk-discount-starts-at-10-items.md
echo "\$ \$EDITOR $DECISION   # accept it and fill in the three sections"
sed -i -e 's/^- Status: proposed/- Status: accepted/' \
  -e 's/^TODO: what forces this decision?/Large orders were paying full price./' \
  -e 's/^TODO: what we decided./10% off the total from 10 items; no discount codes./' \
  -e 's/^TODO: what becomes easier or harder./Simple to explain; tiers can come later./' \
  "$DECISION"
show cat "$DECISION"
show git add -A
show git commit -qm "Record the bulk-discount decision"
go ../../shop
ag queue add record-discount-decision
ag queue run --cleanup
ag gate verify main
ag decision check

echo
ag status
