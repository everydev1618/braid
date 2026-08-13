#!/bin/bash
# The typed-live version of the demo. Each block is one thing you say out loud.
set -e
cd "$(mktemp -d)"
echo "demo dir: $PWD"

# Start from stubs, then let a "founder" agent implement them -- so every definition
# has a recorded intent and the teardown at the end can rebuild all of it.
cat > checkout.py <<'EOF'
TAX = 0.08
CART = [{"price": 20.0, "qty": 1}, {"price": 5.0, "qty": 2}]


def subtotal(cart):
    return 0


def shipping(cart):
    return 0


def total(cart):
    return 0
EOF

braid init checkout.py

cat > founder.py <<'EOF'
TAX = 0.08
CART = [{"price": 20.0, "qty": 1}, {"price": 5.0, "qty": 2}]


def subtotal(cart):
    total = 0
    for item in cart:
        total = total + item["price"] * item["qty"]
    return total


def shipping(cart):
    return 0 if subtotal(cart) > 50 else 5


def total(cart):
    return subtotal(cart) + subtotal(cart) * TAX + shipping(cart)
EOF

braid submit founder.py --id founder --as checkout.py \
      --intent "implement checkout: subtotal, flat shipping free over \$50, and total" \
      --contract "assert abs(subtotal(CART) - 30.0) < 1e-9" \
      --contract "assert total(CART) >= subtotal(CART)"
braid reconcile --apply >/dev/null
rm founder.py

# --- BEAT 1: the no-op ---------------------------------------------------
# same function, every name changed, requoted, reflowed
cat > stylist.py <<'EOF'
TAX = 0.08
CART = [{"price": 20.0, "qty": 1}, {"price": 5.0, "qty": 2}]


def subtotal(basket):
    # accumulate the line items
    running_total = 0

    for line_item in basket:
        running_total = running_total + line_item['price'] * line_item['qty']

    return running_total


def shipping(cart):
    return 0 if subtotal(cart) > 50 else 5


def total(cart):
    return subtotal(cart) + subtotal(cart) * TAX + shipping(cart)
EOF

echo; echo "### git sees this:"; diff -u checkout.py stylist.py | tail -n +3 || true
echo; echo "### braid sees this:"
braid submit stylist.py --id stylist --as checkout.py --intent "rename for clarity"
braid diff stylist
braid abandon stylist

# --- BEAT 2/3: the swarm and the one real conflict -----------------------
sed 's/TAX = 0.08/TAX = 0.095/' checkout.py > a1.py
sed 's/else 5/else 7/' checkout.py > a2.py
cat checkout.py > a3.py; cat >> a3.py <<'EOF'


def item_count(cart):
    return sum(item["qty"] for item in cart)
EOF
sed 's/return subtotal(cart) + subtotal(cart) \* TAX + shipping(cart)/return subtotal(cart) - subtotal(cart) * TAX/' checkout.py > a4.py

braid submit a1.py --id tax-agent   --as checkout.py --intent "update sales tax to 9.5%"
braid submit a2.py --id ship-agent  --as checkout.py --intent "raise flat shipping to \$7"
braid submit a3.py --id count-agent --as checkout.py --intent "add an item_count helper" \
      --contract "assert item_count(CART) == 3"
braid submit a4.py --id rogue-agent --as checkout.py --intent "make total exclude tax and shipping" \
      --contract "assert total(CART) >= subtotal(CART)"

echo; echo "### four agents, one file, no branches:"
braid reconcile --apply

# --- BEAT 4: blame -------------------------------------------------------
echo; echo "### why does this line exist?"
braid blame item_count

# --- ENCORE: the teardown ------------------------------------------------
echo; echo "### delete the code"
rm -f checkout.py *.py
ls
echo; echo "### rebuild it from the recorded intent"
braid rebuild --offline --apply
echo; echo "### and it's back:"
cat checkout.py
