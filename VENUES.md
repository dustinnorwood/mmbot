# Venue economics — verified 2026-07-20

Research basis: official docs fetched today + live API queries today. Source
links inline. Re-check spread/volume numbers on competition day — they rotate.

## Hyperliquid (active MM leg, ~$300) — VERIFIED

### Fees — what you will actually pay
- **Base tier: 1.5 bps maker / 4.5 bps taker** on perps ([fees doc](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)).
- **Maker rebates are unreachable at our size** — they require ≥0.5% of
  exchange-wide 14-day maker volume, not a dollar tier. Plan around *paying*
  1.5 bps per maker fill.
- **Sign up with a referral code before trading**: 4% fee discount (→ 1.44 bps
  maker). HYPE-staking discounts need >10 HYPE staked (~$620 at today's $62) —
  skip at our size.
- No gas per order. **Withdrawal fee: $1.**
- Round trip maker-in/maker-out ≈ **3 bps cost → only quote markets whose
  capturable spread is reliably > 4–5 bps** after adverse selection.

### Mechanics
- **Min order: $10 notional.** With $300 that's ~3–5 quote levels per side of
  $10–30 on one or two markets.
- **ALO (post-only) time-in-force** guarantees you never accidentally take.
  New **"Chase" order type**: post-only that auto-reprices to stay one tick
  off the touch — useful semi-manual.
- No MM program / no permission needed — docs: "anyone is welcome to MM."
- No KYC; auth = wallet EIP-712 signing; agent/API wallets supported.
- Python SDK: `hyperliquid-python-sdk` v0.24.0 (Jun 2026). Testnet live at
  `api.hyperliquid-testnet.xyz` (verified today) with faucet — dry-run there first.

### Rate limits (they shape the bot)
- Address-based: **10,000 free requests, then +1 request per $1 traded**;
  exhausted → throttled to 1 req/10s. A 1Hz requote loop burns 10k in ~3h.
- Mitigations: WebSocket subscriptions for book/fills (not REST polling),
  batch order+cancel actions, requote on price-move thresholds not timers.
  Cancels get expanded headroom (min(limit+100k, 2×limit)) so you can always exit.
- IP REST: 1200 weight/min (l2Book/allMids weight 2, most info weight 20).
  ([rate limits doc](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits))

### Deposits
- Cheapest: **native USDC on Arbitrum → Bridge2**, credited <1 min, cost = cents
  of Arbitrum gas. **MINIMUM 5 USDC — anything less is destroyed, not refunded.**
  Send the whole amount in one tx.
- App now also issues native deposit addresses for USDC on Ethereum/Base/Polygon,
  and CCTP v2 is live via HyperEVM (since Sep 2025). Per-route minimums not
  documented — check the app's displayed minimum before sending.

### Market candidates (live snapshot 2026-07-20 — RE-CHECK on day 1)
Queue at touch on these was only $150–$1,500, so a $30 order competes.

| Market | 24h vol | TOB spread | Max lev | Note |
|---|---|---|---|---|
| CASHCAT | $25.9M | **19.9 bps** | 3x | widest; memecoin-ish, brutal adverse selection — widen quotes |
| ACE | $8.5M | **16.6 bps** | 3x | same caveat |
| FARTCOIN | $8.7M | 8.7 bps | 10x | middle ground |
| KAITO | $7.4M | 5.3 bps | 5x | middle ground |
| ONDO | $10.5M | 5.1 bps | 10x | middle ground |
| VVV | $2.8M | 4.3 bps | 3x | thin volume |
| ENA | $4.2M | 4.1 bps | 10x | |
| LIT | $18.8M | 3.6 bps | 5x | near the 3 bps cost floor |

**Avoid**: CRV, JTO, TRUMP, TAO — mid-cap volume but sub-1 bp spreads = pro
makers already own them. Avoid BTC/ETH majors entirely.

### Structural changes since early 2026
- **HIP-3 builder-deployed perp dexes** live (9 separate order books — the
  `#XXXX` markets in allMids). Deployer-set extra fees; stay on the main dex.
- Portfolio margin + USDC borrow/lend (Dec 2025) — not needed at our size.
- USDH-quoted markets: 20% lower taker fees / better rebate treatment —
  rebates still unreachable, minor.

### Caveats
- Spread/volume figures are a single-day snapshot; mid-cap spreads move
  intraday and new listings rotate weekly.
- Deposit minimums for non-Arbitrum USDC routes are shown in-app only.

---

## Base LP leg (~$150) — VERIFIED (live DefiLlama/BaseScan/Deribit data 2026-07-20)

### Gas: a non-issue
Base is at its 0.005 gwei floor (BaseScan, timestamped today). Entire position
lifecycle — approvals, mint, 2–3 rebalances, collect, burn — costs **$0.05–0.50
total**. Frequent re-centering is economically free. (Ignore QuickNode's
"8.2 gwei" tracker — stale/broken, contradicted by BaseScan + Base docs.)

### Venue: Uniswap v4 is the boring-reliable pick — Aerodrome is mid-upheaval
- **Aerodrome is executing its merger into "Aero" THIS MONTH**: mandatory LP
  migration to new MEV-resistant pools (unmigrated positions stop earning
  emissions), dynamic fees, gauge-voting replacement. UI/contract churn during
  exactly our competition week. Risk — or opportunity: the new migration pools
  show transient triple-digit reward APRs because emissions moved before TVL did.
- **Uniswap v4 has overtaken v3 on Base by volume** (~$262M/day vs $124M) and is
  the app's default create-position flow. Native ETH pairs (no wrap). For a
  small position: pick a **vanilla no-hook pool** (a malicious hook can extract
  from LPs — check before depositing).
- Aerodrome Slipstream fee mechanics if used: **staked in gauge = AERO emissions
  only (fees go to voters); unstaked = swap fees minus 10%**. Choose based on
  what you want to show in the presentation.

### Candidate pools (live 2026-07-20; vol/TVL is the trustworthy signal)

| Pool | Venue | vol/TVL (24h) | Fee APY | Reward APR | Read |
|---|---|---|---|---|---|
| USDC/cbBTC 0.05% | Uni v4 | 1.5 | ~27% | — | **safe default** — BTC DVOL only 36 → best fee/IL ratio |
| ETH/cbBTC 0.05% | Uni v4 | 2.0 | ~37% | — | correlated pair → damped IL |
| WETH/USDC CL100 | Slipstream | 5.1 | ~13% | 25% | volume king; unstake = fees −10% |
| WETH/USDC CL50 *(new)* | Slipstream | 7.1 | (267%) | (746%) | migration pool — APRs transient, AERO price risk |
| EURC/USDC CL50 | Slipstream | 1.7 | ~25% | 3.5% | near-zero IL floor (FX vol only) |

### Expected math for $150 over ~5 days (ETH/USDC, ±10% range)
ETH DVOL ≈ 50 → 5-day σ ≈ 5.9%. ±10% range ≈ 21× concentration; expected IL
≈ $1.30–1.40. Expected fees: Uni v4 ETH/USDC $0.30–1.00; Slipstream CL100
unstaked $0.50–3.00; hot migration pools $3–15 if APRs hold (they'll compress).
**On mature pools, fees ≈ IL at current vol — near break-even ex-rewards.**
Edge comes from: high vol/TVL pools, cbBTC-quoted pairs (lower vol), or
transient emissions. A tight ±3% range exits within 1–2 days at current vol —
viable only with active re-centering (which gas makes free — and "active range
management" is the better presentation story anyway).

### Tracking
**Revert Finance is alive** and supports Base + Uniswap v4 + Aerodrome: per-
position fees, PnL, divergence loss. Use it alongside `mm lp add` snapshots.

### Market context (competition week)
ETH ≈ $1,894, BTC ≈ $65,400, ETH DVOL ≈ 50 (vs BTC 36) — options flow is
pricing an outsized ETH move this month. Wider ranges / wider quotes than
you'd naively pick.

---

## Bridging ($500 scale) — VERIFIED (live Across API quotes 2026-07-20)

**Rule 1: don't start a hop on Ethereum mainnet — origin gas ($1–3) dwarfs
every bridge fee at this size.** If funds come from an exchange, withdraw
directly to Arbitrum or Base.

| Route | Best option | Cost on $500 | Time |
|---|---|---|---|
| Arbitrum → Hyperliquid | native Bridge2 deposit | gas only (<$0.05) | ~1 min |
| Base → Hyperliquid | **Across direct-to-HyperCore route** (live since Dec 2025) | ~$0.05–0.15 | 8–20 s |
| L2 ↔ L2 (Arb/Base) | Across | ~$0.07 (1.4 bps) | seconds |
| Any CCTP chain → any | CCTP v2 Standard (via Jumper/Bungee/Across UI) | **$0** protocol fee | source finality (Eth ~15 min) |
| — | Stargate | ~6 bps — strictly worse than Across here | — |

Reminders: Hyperliquid min deposit **5 USDC (below = destroyed)**; withdrawal
fee 1 USDC; new HyperCore accounts pay a one-time 1 USDC activation via the
CCTP route. Log every hop in `mm ledger` with the tx hash.

---

## Venue comparison (active MM leg) — VERIFIED 2026-07-20

**Headline: Hyperliquid does NOT win on maker fees.** Three serious venues
charge 0% maker at base tier with open APIs:

| Venue | Maker | Taker | 24h vol | API | Notes |
|---|---|---|---|---|---|
| **Hyperliquid** | 0.015% | 0.045% | $1.85B | open, permissionless | deepest liquidity, best tooling/testnet, no points |
| **Lighter** | **0%** | **0%** | $1.33B (but see deep-dive below) | open (Standard acct) | 200–300 ms latency floor on Standard; points program status stale/unverified |
| **Aster** | **0%** | 0.04% | $636M | open, Binance-compatible | deepest of the zero-maker group; airdrop program ended Mar 2026 |
| **Extended** | **0%** | 0.025% | $205M | open per docs (UNVERIFIED approval) | rebates only at ≥0.5% exchange share |
| Paradex | 0% retail | 0% retail | $2.6M | retail API capped 3 orders/s, **1,000 orders/day** — impractical for MM | XP Season 3 active; thin volume |
| dYdX v4 | ~0.01% (docs/trackers conflict) | 0.05% | $7.3M | open | Surge S13 incentives; volume collapsed |
| Drift → "Velocity" | — | — | ~$0 | **offline** | hacked $285M Apr 2026 (DPRK-attributed); private beta relaunch |

### The decision at $300 scale
- Hyperliquid's 1.5 bps maker fee = **3 bps per maker round trip**. On a 5 bps
  market that's 60% of gross edge gone to fees; on Lighter/Aster it's 0%.
- Lighter's catch: the Standard (free) tier has a 200 ms maker-cancel /
  300 ms taker latency floor, while pro makers run Premium with lower latency —
  i.e., you're structurally slower to pull quotes on fast moves and eat more
  adverse selection. That's the hidden price of "free." (It also caps how bad
  HFT sniping can be — everyone fast pays fees.)
- Hyperliquid's case: 2.4× the liquidity, mature SDK + live testnet faucet for
  a dry run, richest public data (our toolkit is already verified against it).
- Aster's case: zero maker + real volume, Binance-compatible API (familiar
  shape), but tooling/testnet story weaker and no current incentive program.

**Sane framings for the week:** (a) run the active leg on a zero-maker venue
(Lighter or Aster) and keep Hyperliquid for its testnet dry-run + as the data
benchmark, or (b) stay on Hyperliquid for execution quality and restrict
quoting to markets with >5 bps of reliable spread so fees stay <40% of gross.
Either way the fee-economics comparison is strong presentation material.

---

## Lighter deep-dive — VERIFIED 2026-07-20 (live API probes + official docs)

Verification pass on the split-decision facts. Key changes vs the summary above:

### Corrections to earlier claims
- **Points Season 2 is OVER** (distributions completed ~Dec 27 2025; LIT TGE +
  airdrop Dec 30 2025). The official docs still carry Season 2 text — they are
  stale, which is what misled the earlier comparison. No Season 3 announced as
  of latest verifiable reporting (Mar 2026). **Do not count points as yield.**
- **Volume concentration is extreme**: $1.33B/day exchange-wide, but
  **BTC+ETH = 81%** of it. The mid-caps we'd quote do a tiny fraction of their
  Hyperliquid volume:

| Market | Lighter 24h vol | Hyperliquid 24h vol | Lighter spread (snapshot) |
|---|---|---|---|
| FARTCOIN | $302k | $8.7M | 10.2 bps |
| KAITO | $302k | $7.4M | 22.2 bps |
| ONDO | $161k | $10.5M | 8.7 bps |
| ENA | $305k | $4.2M | 6.0 bps |

  ~25–65× less flow on our target markets. Zero fees don't pay when nothing
  trades: expect low fill rates. CASHCAT and ACE are not listed at all.
- **The latency floor is worse than it sounded**: Standard cancels wait 200 ms
  while Premium makers cancel at 0 ms and staked Premium takers hit at 140 ms —
  a fast taker beats your cancel by ~60 ms *every time*. Structural stale-quote
  sniping risk with no fee cushion.

### Confirmed facts (favorable)
- **0 maker / 0 taker verified in live per-market data on all 218 markets**,
  not just docs. 201 active markets incl. FX, equities, commodities perps.
- **$10 min order notional** (binding constraint, verified live); min deposit
  5 USDC via CCTP from Base/Arbitrum (1 USDC via Ethereum L1). No min account.
- Withdrawals: standard zk exit ≈53 min (live-probed); fast withdrawal min
  4 USDC (fee UNVERIFIED). Deposit rails include HyperEVM.
- **Read-only integration is trivial**: account state/positions and candles
  need NO credentials (`GET /api/v1/account?by=l1_address&value=0x…`,
  `/api/v1/candles`); own-fills need a read-only auth token (`ro:` tokens,
  cannot trade/withdraw) or the no-auth `recentTrades` filter-by-account trick.
  Python SDK: `pip install lighter-sdk` (v1.1.2). Testnet exists
  (`testnet.zklighter.elliot.ai`, verified live).
- **Self-trade prevention**: self-matches never execute; default STP mode
  switched to `cancel_maker` (May 31 2026) — a two-sided quoter must set STP
  explicitly or a crossing repost silently kills its own resting quote
  (dangerous combined with the 200 ms cancel delay).

### Net read for the split
The experiment reframes from "fees vs no fees" to **"flow vs fees"**: deep
book that taxes you (Hyperliquid) vs free venue where your target markets
barely trade (Lighter). Still a legitimate A/B — arguably a more honest MM
lesson — but expect the Lighter leg to fill rarely, and don't allocate it
capital it can't use (10x leverage available; margin is not the constraint).
