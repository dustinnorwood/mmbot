"""mmkit CLI. Run as `python -m mmkit <command>` (alias it to `mm`).

Commands:
  config     set/show defaults (address, testnet)
  ping       connectivity check against the Hyperliquid info API
  sync       pull fills for the account into the local DB
  pnl        PnL attribution table
  markout    adverse-selection table (1m / 5m by default)
  risk       live positions + margin from the venue (flatten check)
  ledger     fund-flow log: add / list
  lp         LP position snapshots: add / list
  report     write the full markdown report to reports/
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .analytics import attribute_pnl, compute_markouts
from .hl import HLClient, HLError
from .lighter import LighterClient, LighterError, trade_to_fill
from .report import build_report

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"

# Coins are displayed/analyzed as "TAG:SYMBOL" so the same symbol on two
# venues (e.g. FARTCOIN on both books) never collides in attribution.
VENUE_TAGS = {"hyperliquid": "HL", "lighter": "LT"}
TAG_VENUES = {v: k for k, v in VENUE_TAGS.items()}


def _tag_fills(rows) -> list:
    out = []
    for r in rows:
        d = dict(r)
        d["coin"] = f"{VENUE_TAGS.get(r['venue'], r['venue'])}:{r['coin']}"
        out.append(d)
    return out


def _parse_tag(tagged: str):
    tag, _, sym = tagged.partition(":")
    return TAG_VENUES.get(tag, "hyperliquid"), (sym or tag)


def _client(args) -> HLClient:
    cfg = db.load_config()
    testnet = getattr(args, "testnet", False) or cfg.get("testnet", False)
    return HLClient(testnet=testnet)


def _address(args) -> str:
    addr = getattr(args, "address", None) or db.load_config().get("address")
    if not addr:
        sys.exit("No address: pass --address 0x… or set one with `mm config set address 0x…`")
    return addr


def _parse_since(s):
    if s is None:
        return None
    try:
        return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        sys.exit(f"--since must be YYYY-MM-DD, got {s!r}")


# ------------------------------------------------------------------ commands
def cmd_config(args):
    cfg = db.load_config()
    if args.action == "show" or not args.key:
        for k, v in cfg.items():
            print(f"{k} = {v}")
        if not cfg:
            print("(empty — try `mm config set address 0x…`)")
        return
    if args.action == "set":
        val = args.value
        if val is not None and val.lower() in ("true", "false"):
            val = val.lower() == "true"
        cfg[args.key] = val
        db.save_config(cfg)
        print(f"set {args.key} = {val}")


def cmd_ping(args):
    client = _client(args)
    mids = client.all_mids()
    net = "testnet" if client.base_url.find("testnet") >= 0 else "mainnet"
    sample = ", ".join(f"{c}={p:g}" for c, p in list(sorted(mids.items()))[:3])
    print(f"ok: {net} info API returned {len(mids)} mids ({sample}, …)")


def _lighter_client(args) -> LighterClient:
    cfg = db.load_config()
    testnet = getattr(args, "testnet", False) or cfg.get("testnet", False)
    return LighterClient(testnet=testnet)


def _lighter_account_index(args, lclient: LighterClient) -> int:
    cfg = db.load_config()
    idx = cfg.get("lighter_account_index")
    if idx is not None:
        return int(idx)
    l1 = cfg.get("lighter_address")
    if not l1:
        sys.exit("set your Lighter L1 wallet first: `mm config set lighter_address 0x…`")
    accounts = lclient.accounts_by_l1(l1)
    if not accounts:
        sys.exit(f"no Lighter account found for {l1} — deposit first to create one")
    a = accounts[0]
    idx = int(a.get("index", a.get("account_index")))
    cfg["lighter_account_index"] = idx
    db.save_config(cfg)
    print(f"resolved Lighter account index {idx} (cached in config)")
    return idx


def _sync_lighter(args, conn) -> None:
    lclient = _lighter_client(args)
    idx = _lighter_account_index(args, lclient)
    fills = []
    if lclient.auth_token:
        # reliable path: cursor-paginated own-trade history
        id2sym = {int(v["market_id"]): k for k, v in lclient.markets().items()}
        known_since = db.latest_fill_time(conn, "lighter") or 0
        cursor = None
        while True:
            page = lclient.my_trades_auth(idx, limit=100, cursor=cursor)
            trades = page.get("trades", [])
            for t in trades:
                sym = id2sym.get(int(t.get("market_id", -1)), str(t.get("market_id")))
                f = trade_to_fill(t, idx, sym)
                if f:
                    fills.append(f)
            cursor = page.get("next_cursor") or page.get("cursor")
            if not trades or not cursor:
                break
            if min(int(t["timestamp"]) for t in trades) < known_since - 3_600_000:
                break  # paginated past everything we already have (1h overlap)
    else:
        markets = [m.strip() for m in str(db.load_config().get("lighter_markets", "")).split(",") if m.strip()]
        if not markets:
            sys.exit("set the markets you quote: `mm config set lighter_markets FARTCOIN,KAITO`")
        for sym in markets:
            for t in lclient.recent_trades(sym, limit=100):
                f = trade_to_fill(t, idx, sym)
                if f:
                    fills.append(f)
    n_new = db.insert_fills(conn, fills, venue="lighter")
    total = conn.execute("SELECT COUNT(*) FROM fills WHERE venue='lighter'").fetchone()[0]
    print(f"lighter: fetched {len(fills)} own fills, {n_new} new (db total: {total})")
    if not lclient.auth_token:
        print("note: no LIGHTER_AUTH_TOKEN — recentTrades only covers the last 100 trades")
        print("      per market; sync every few hours, or mint a read-only 'ro:' token.")


def cmd_sync(args):
    conn = db.connect()
    if args.venue == "lighter":
        return _sync_lighter(args, conn)
    addr = _address(args)
    client = _client(args)
    since = _parse_since(args.since)
    if since is None:
        last = db.latest_fill_time(conn, "hyperliquid")
        # overlap 1h on incremental sync; default lookback 14d on first run
        since = (last - 3_600_000) if last else int(time.time() * 1000) - 14 * 86_400_000
    fills = client.user_fills_by_time(addr, since)
    n_new = db.insert_fills(conn, fills, venue="hyperliquid")
    total = conn.execute("SELECT COUNT(*) FROM fills WHERE venue='hyperliquid'").fetchone()[0]
    print(f"hyperliquid: fetched {len(fills)} fills, {n_new} new (db total: {total})")


def _mid_lookup(conn, args, fills, horizons_s):
    """Ensure 1m candle coverage for every (venue, coin) fill window, then
    return a lookup fn over tagged coins ("HL:X" / "LT:X")."""
    if not fills:
        return lambda coin, t: None
    now_ms = int(time.time() * 1000)
    by_key = {}
    for f in fills:
        by_key.setdefault(_parse_tag(f["coin"]), []).append(int(f["time_ms"]))
    max_h_ms = max(horizons_s) * 1000
    hl_client = lt_client = None
    for (venue, sym), times in by_key.items():
        lo = min(times) - 120_000
        hi = min(max(times) + max_h_ms + 120_000, now_ms)
        cov = db.candle_coverage(conn, sym, venue)
        if cov and cov[0] <= lo and cov[1] >= (hi // 60_000) * 60_000 - 60_000:
            continue  # cache already covers the window
        if venue == "lighter":
            lt_client = lt_client or _lighter_client(args)
            candles = lt_client.candles_1m(sym, lo, hi)
        else:
            hl_client = hl_client or _client(args)
            candles = hl_client.candles(sym, "1m", lo, hi)
        db.upsert_candles(conn, sym, candles, venue)

    def mid_at(tagged: str, t_ms: int):
        if t_ms > now_ms:
            return None  # horizon hasn't elapsed yet
        venue, sym = _parse_tag(tagged)
        return db.get_close_at(conn, sym, t_ms, venue)

    return mid_at


def _collect_marks(args, tagged_fills) -> dict:
    """Current marks per tagged coin: HL from allMids, Lighter from last 1m close."""
    marks = {}
    hl_syms = sorted({_parse_tag(f["coin"])[1] for f in tagged_fills
                      if _parse_tag(f["coin"])[0] == "hyperliquid"})
    lt_syms = sorted({_parse_tag(f["coin"])[1] for f in tagged_fills
                      if _parse_tag(f["coin"])[0] == "lighter"})
    if hl_syms:
        try:
            mids = _client(args).all_mids()
            for s in hl_syms:
                if s in mids:
                    marks[f"HL:{s}"] = mids[s]
        except HLError as e:
            print(f"warning: no HL marks ({e})", file=sys.stderr)
    if lt_syms:
        try:
            lc = _lighter_client(args)
            for s in lt_syms:
                px = lc.latest_close(s)
                if px is not None:
                    marks[f"LT:{s}"] = px
        except LighterError as e:
            print(f"warning: no Lighter marks ({e})", file=sys.stderr)
    return marks


def cmd_markout(args):
    conn = db.connect()
    horizons = [int(h) for h in args.horizons.split(",")]
    fills = _tag_fills(db.get_fills(conn, coin=args.coin,
                                    since_ms=_parse_since(args.since),
                                    venue=getattr(args, "venue", None)))
    if not fills:
        sys.exit("no fills in db — run `mm sync` first")
    mid_at = _mid_lookup(conn, args, fills, horizons)
    rows = compute_markouts(fills, mid_at, horizons)
    print(f"{'coin':<10} {'role':<6} {'hzn':>5} {'n':>5} {'avg bps':>9} {'total $':>10} {'miss':>5}")
    for r in rows:
        role = "maker" if r.maker else "taker"
        print(
            f"{r.coin:<10} {role:<6} {r.horizon_s:>4}s {r.n:>5} "
            f"{r.avg_bps:>+9.2f} {r.sum_usd:>+10.2f} {r.n_missing:>5}"
        )
    print("\nnegative maker bps = adverse selection; compare against your half-spread")


def cmd_pnl(args):
    conn = db.connect()
    fills = _tag_fills(db.get_fills(conn, coin=args.coin,
                                    since_ms=_parse_since(args.since),
                                    venue=getattr(args, "venue", None)))
    if not fills:
        sys.exit("no fills in db — run `mm sync` first")
    marks = _collect_marks(args, fills)
    pnl = attribute_pnl(fills, marks)
    hdr = f"{'coin':<10} {'fills':>6} {'mkr%':>5} {'volume$':>10} {'realized':>9} {'unreal':>9} {'fees':>7} {'rebate':>7} {'net':>9} {'pos':>10}"
    print(hdr)
    tot_net = tot_fees = tot_reb = 0.0
    for c in sorted(pnl.values(), key=lambda c: -abs(c.net)):
        mkr = 100.0 * c.n_maker / c.n_fills if c.n_fills else 0
        print(
            f"{c.coin:<10} {c.n_fills:>6} {mkr:>4.0f}% {c.volume_usd:>10,.0f} "
            f"{c.realized:>+9.2f} {c.unrealized:>+9.2f} {c.fees_paid:>7.2f} "
            f"{c.rebates:>7.2f} {c.net:>+9.2f} {c.position:>10.4f}"
        )
        tot_net += c.net
        tot_fees += c.fees_paid
        tot_reb += c.rebates
        if abs(c.venue_realized - c.realized) > max(1.0, 0.02 * abs(c.realized)):
            print(f"  note: venue-reported realized for {c.coin} is {c.venue_realized:+.2f} (avg-cost calc differs; partial history?)")
    print(f"\ntrading net: {tot_net:+.2f}  (fees {tot_fees:.2f}, rebates {tot_reb:.2f})")
    led = conn.execute(
        "SELECT COALESCE(SUM(amount_usd),0) FROM ledger WHERE category IN ('gas','bridge_fee')"
    ).fetchone()[0]
    if led:
        print(f"gas+bridge from ledger: {led:+.2f}   ALL-IN: {tot_net + led:+.2f}")


def cmd_risk(args):
    if getattr(args, "venue", None) == "lighter":
        lclient = _lighter_client(args)
        idx = _lighter_account_index(args, lclient)
        acct = lclient.account(idx)
        print(f"collateral:     {float(acct.get('collateral', 0)):,.2f} USDC")
        print(f"available:      {float(acct.get('available_balance', 0)):,.2f} USDC")
        poss = [p for p in acct.get("positions", []) if float(p.get("position", 0)) != 0]
        if not poss:
            print("positions:      FLAT ✓")
            return
        print("positions:")
        for p in poss:
            sign = int(p.get("sign", 1))
            sz = float(p.get("position", 0)) * (1 if sign >= 0 else -1)
            print(
                f"  {p.get('symbol'):<10} {sz:>+12.4f}  entry {float(p.get('avg_entry_price') or 0):>10.4f}"
                f"  uPnL {float(p.get('unrealized_pnl', 0)):>+8.2f}  (flatten before EOD!)"
            )
        return
    addr = _address(args)
    client = _client(args)
    st = client.clearinghouse_state(addr)
    ms = st.get("marginSummary", {})
    print(f"account value:  {float(ms.get('accountValue', 0)):,.2f} USDC")
    print(f"total ntl pos:  {float(ms.get('totalNtlPos', 0)):,.2f} USDC")
    print(f"withdrawable:   {float(st.get('withdrawable', 0)):,.2f} USDC")
    poss = st.get("assetPositions", [])
    if not poss:
        print("positions:      FLAT ✓")
        return
    print("positions:")
    for p in poss:
        pos = p.get("position", {})
        szi = float(pos.get("szi", 0))
        if szi == 0:
            continue
        upnl = float(pos.get("unrealizedPnl", 0))
        print(
            f"  {pos.get('coin'):<10} {szi:>+12.4f}  entry {float(pos.get('entryPx') or 0):>10.4f}"
            f"  uPnL {upnl:>+8.2f}  (flatten before EOD!)"
        )


def cmd_ledger(args):
    conn = db.connect()
    if args.action == "add":
        if args.category not in db.LEDGER_CATEGORIES:
            sys.exit(f"category must be one of: {', '.join(db.LEDGER_CATEGORIES)}")
        ts = args.ts or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        conn.execute(
            "INSERT INTO ledger (ts, chain, tx_hash, amount_usd, category, note) VALUES (?,?,?,?,?,?)",
            (ts, args.chain, args.tx, args.amount, args.category, args.note),
        )
        conn.commit()
        print(f"logged: {ts} {args.chain} {args.category} {args.amount:+.2f} {args.note or ''}")
    else:
        rows = conn.execute("SELECT * FROM ledger ORDER BY ts").fetchall()
        if not rows:
            print("(ledger empty)")
            return
        total = 0.0
        for r in rows:
            tx = f"  {r['tx_hash']}" if r["tx_hash"] else ""
            print(f"{r['ts']}  {r['chain']:<10} {r['category']:<11} {r['amount_usd']:>+9.2f}  {r['note'] or ''}{tx}")
            total += r["amount_usd"]
        print(f"{'':<34}net {total:>+9.2f}")


def cmd_lp(args):
    conn = db.connect()
    if args.action == "add":
        ts = args.ts or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        rng = args.range or (None, None)
        conn.execute(
            """INSERT INTO lp_snapshots (ts, venue, pool, value_usd, fees_earned_usd,
                                         range_lo, range_hi, in_range, note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, args.venue, args.pool, args.value, args.fees, rng[0], rng[1],
             None if args.in_range is None else int(args.in_range), args.note),
        )
        conn.commit()
        print(f"snapshot: {ts} {args.venue} {args.pool} value={args.value:.2f} fees={args.fees}")
    else:
        rows = conn.execute("SELECT * FROM lp_snapshots ORDER BY ts").fetchall()
        if not rows:
            print("(no LP snapshots)")
            return
        for r in rows:
            rng = f" [{r['range_lo']:g}–{r['range_hi']:g}]" if r["range_lo"] is not None else ""
            inr = "" if r["in_range"] is None else ("  in-range" if r["in_range"] else "  OUT-OF-RANGE")
            fees = f" fees={r['fees_earned_usd']:.2f}" if r["fees_earned_usd"] is not None else ""
            print(f"{r['ts']}  {r['venue']:<10} {r['pool']:<18} ${r['value_usd']:>9.2f}{fees}{rng}{inr}")


def cmd_report(args):
    conn = db.connect()
    fills = _tag_fills(db.get_fills(conn, since_ms=_parse_since(args.since)))
    client = _client(args)
    positions_note = ""
    marks = _collect_marks(args, fills) if fills else {}
    pnl = attribute_pnl(fills, marks) if fills else {}
    markouts = []
    if fills:
        try:
            mid_at = _mid_lookup(conn, args, fills, [60, 300])
            markouts = compute_markouts(fills, mid_at, [60, 300])
        except (HLError, LighterError) as e:
            print(f"warning: markouts skipped: {e}", file=sys.stderr)
    inv_bits = []
    addr = getattr(args, "address", None) or db.load_config().get("address")
    if addr:
        try:
            st = client.clearinghouse_state(addr)
            open_pos = [
                p["position"] for p in st.get("assetPositions", [])
                if float(p.get("position", {}).get("szi", 0)) != 0
            ]
            if open_pos:
                items = ", ".join(f"{p['coin']} {float(p['szi']):+g}" for p in open_pos)
                inv_bits.append(f"⚠️ HL: {items}")
            else:
                inv_bits.append("HL: flat ✓")
        except HLError:
            pass
    if db.load_config().get("lighter_account_index") is not None:
        try:
            lclient = _lighter_client(args)
            acct = lclient.account(int(db.load_config()["lighter_account_index"]))
            lt_pos = [p for p in acct.get("positions", []) if float(p.get("position", 0)) != 0]
            if lt_pos:
                items = ", ".join(
                    f"{p.get('symbol')} {float(p.get('position', 0)) * (1 if int(p.get('sign', 1)) >= 0 else -1):+g}"
                    for p in lt_pos
                )
                inv_bits.append(f"⚠️ LT: {items}")
            else:
                inv_bits.append("LT: flat ✓")
        except LighterError:
            pass
    if inv_bits:
        note = " · ".join(inv_bits)
        warn = " — remember the flatten rule." if "⚠️" in note else ""
        positions_note = f"## Open inventory right now\n\n{note}{warn}"
    ledger_rows = conn.execute("SELECT * FROM ledger ORDER BY ts").fetchall()
    by_cat = {
        r["category"]: r["s"]
        for r in conn.execute("SELECT category, SUM(amount_usd) AS s FROM ledger GROUP BY category")
    }
    lp_rows = conn.execute("SELECT * FROM lp_snapshots ORDER BY ts").fetchall()

    md = build_report(fills, pnl, markouts, ledger_rows, by_cat, lp_rows, addr, positions_note)
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}.md"
    out.write_text(md)
    print(md)
    print(f"→ written to {out}")


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mm", description="market-making competition toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, venue_default=None):
        sp.add_argument("--address", help="account address (default from config)")
        sp.add_argument("--testnet", action="store_true", help="use testnet API")
        sp.add_argument("--since", help="YYYY-MM-DD (UTC) lower bound")
        sp.add_argument("--venue", choices=["hyperliquid", "lighter"], default=venue_default,
                        help="restrict to one venue" + (f" (default {venue_default})" if venue_default else " (default: all)"))

    sp = sub.add_parser("config", help="show/set defaults")
    sp.add_argument("action", choices=["show", "set"], nargs="?", default="show")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("ping", help="check info API connectivity")
    sp.add_argument("--testnet", action="store_true")
    sp.set_defaults(fn=cmd_ping)

    sp = sub.add_parser("sync", help="pull fills into local db")
    add_common(sp, venue_default="hyperliquid")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("pnl", help="PnL attribution")
    add_common(sp)
    sp.add_argument("--coin")
    sp.set_defaults(fn=cmd_pnl)

    sp = sub.add_parser("markout", help="adverse-selection analysis")
    add_common(sp)
    sp.add_argument("--coin")
    sp.add_argument("--horizons", default="60,300", help="comma-separated seconds (default 60,300)")
    sp.set_defaults(fn=cmd_markout)

    sp = sub.add_parser("risk", help="live positions / flatten check")
    add_common(sp)
    sp.set_defaults(fn=cmd_risk)

    sp = sub.add_parser("ledger", help="fund-flow ledger")
    lsub = sp.add_subparsers(dest="action", required=True)
    la = lsub.add_parser("add")
    la.add_argument("--chain", required=True)
    la.add_argument("--amount", type=float, required=True, help="USD; negative = cost/outflow")
    la.add_argument("--category", required=True, help="|".join(db.LEDGER_CATEGORIES))
    la.add_argument("--tx", help="transaction hash")
    la.add_argument("--note")
    la.add_argument("--ts", help='override timestamp "YYYY-MM-DD HH:MM" UTC')
    la.set_defaults(fn=cmd_ledger)
    ll = lsub.add_parser("list")
    ll.set_defaults(fn=cmd_ledger)

    sp = sub.add_parser("lp", help="LP position snapshots")
    psub = sp.add_subparsers(dest="action", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("--venue", required=True)
    pa.add_argument("--pool", required=True)
    pa.add_argument("--value", type=float, required=True, help="position value USD if withdrawn now")
    pa.add_argument("--fees", type=float, help="cumulative fees earned USD")
    pa.add_argument("--range", nargs=2, type=float, metavar=("LO", "HI"))
    pa.add_argument("--in-range", dest="in_range", action="store_true", default=None)
    pa.add_argument("--out-of-range", dest="in_range", action="store_false")
    pa.add_argument("--note")
    pa.add_argument("--ts", help='override timestamp "YYYY-MM-DD HH:MM" UTC')
    pa.set_defaults(fn=cmd_lp)
    pl = psub.add_parser("list")
    pl.set_defaults(fn=cmd_lp)

    sp = sub.add_parser("report", help="write full markdown report")
    add_common(sp)
    sp.set_defaults(fn=cmd_report)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except HLError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
