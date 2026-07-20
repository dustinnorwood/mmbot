# MM Competition Toolkit

Tracking tooling for a one-week, $500 market-making competition. Zero external
dependencies — Python 3.9+ stdlib only. Data lives in `data/mmkit.db` (SQLite);
reports render to `reports/`.

## Setup

```bash
alias mm='python3 -m mmkit'          # add to your shell rc for the week
mm ping                              # confirm the Hyperliquid info API is reachable
mm config set address 0xYOURADDRESS  # your trading wallet (public data only — no keys)
```

Everything is read-only against the venue: the tool never holds keys and never
places orders. It observes, attributes, and reports.

Two-venue setup (the $300 HL / $100 Lighter split):

```bash
mm config set lighter_address 0xYOURWALLET     # L1 wallet that deposited to Lighter
mm config set lighter_markets FARTCOIN,KAITO   # markets you quote there
```

## Daily workflow

```bash
mm sync                # pull new Hyperliquid fills (incremental, idempotent)
mm sync --venue lighter  # pull Lighter fills — every few hours (see note below)
mm pnl                 # attribution per venue: HL:COIN vs LT:COIN rows
mm markout             # adverse-selection check — THE health metric
mm risk                # HL open inventory — flatten before end of day
mm risk --venue lighter  # same for Lighter
mm report              # full markdown snapshot → reports/YYYY-MM-DD.md
```

Lighter fills note: without credentials, sync reads each market's last 100
public trades and filters for your account — fine if you sync every few hours
in these thin markets. For guaranteed completeness, mint a **read-only** auth
token (`ro:…`, via lighter-sdk — it cannot trade or withdraw) and export it as
`LIGHTER_AUTH_TOKEN`; sync then uses the paginated own-trades endpoint.

Log every fund movement the moment it happens (this is your traceability story):

```bash
mm ledger add --chain base     --category deposit    --amount 500    --tx 0x… --note "competition funds in"
mm ledger add --chain base     --category bridge_fee --amount -0.85  --tx 0x… --note "CCTP base→arbitrum"
mm ledger add --chain arbitrum --category gas        --amount -0.12  --tx 0x…
mm ledger list
```

Snapshot the passive LP leg whenever you check it (2×/day is plenty):

```bash
mm lp add --venue aerodrome --pool "WETH/USDC 0.05%" --value 149.20 --fees 0.41 \
          --range 3400 3800 --in-range
mm lp list
```

## Reading the numbers

- **PnL attribution** — `realized` on a flat book ≈ spread capture. If realized
  is positive but net is negative, fees or inventory drift ate your edge; the
  table shows which.
- **Markout** — average PnL per fill measured 60s and 300s later. Negative
  maker markouts mean informed flow picks you off; if |markout| exceeds your
  half-spread, widen your quotes or requote faster. This is the single most
  presentation-worthy chart you'll produce.
- **Risk** — the flatten check. A market maker carrying overnight inventory is
  a directional trader with extra steps.

## The quoter (order placement — separate from tracking)

`quoter/` places actual orders; it is the only code that touches a key, and it
only ever loads an **agent (API) wallet key** — approved by your master wallet,
able to trade, unable to withdraw. Setup:

```bash
python3 -m venv .venv && .venv/bin/pip install hyperliquid-python-sdk
mkdir -p ~/.mmkit && chmod 700 ~/.mmkit
read -rs KEY && echo "$KEY" > ~/.mmkit/hl_agent_key && chmod 600 ~/.mmkit/hl_agent_key && unset KEY
cp quoter.example.json quoter.json   # edit: account_address, coin, sizes/limits
```

Run order (testnet is the default everywhere; mainnet is a flag + confirmation):

```bash
.venv/bin/python -m quoter run --dry-run        # print quotes, send nothing
.venv/bin/python -m quoter run                  # live on TESTNET
.venv/bin/python -m quoter status               # equity / position / open orders
.venv/bin/python -m quoter flatten              # cancel all + close position
.venv/bin/python -m quoter run --mainnet        # real funds, asks for confirmation
```

Strategy: post-only (ALO) ladder around an inventory-skewed reservation price
with vol-scaled half-spread. Safety rails: max position (one-sided quoting at
the cap), max daily loss halt, `STOP` kill file, cancel-all on any exit,
requote-on-threshold to respect rate limits. Tune everything in `quoter.json`.

## Testing

```bash
python3 -m unittest discover -s tests -v        # mmkit tests (stdlib)
.venv/bin/python -m unittest discover -s tests  # + quoter engine tests
```

## Layout

```
mmkit/hl.py         Hyperliquid public info API client (read-only, no keys)
mmkit/db.py         SQLite schema + storage helpers
mmkit/analytics.py  avg-cost PnL attribution, markout math   ← unit-tested core
mmkit/report.py     markdown assembly
mmkit/cli.py        the `mm` command
tests/              synthetic-fill tests for the analytics core
```
