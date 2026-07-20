"""Markdown report assembly — the artifact you'll build the presentation from."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .analytics import CoinPnL, MarkoutRow


def _fmt_usd(x: float) -> str:
    return f"{x:+,.2f}" if x else "0.00"


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def pnl_section(pnl: Dict[str, CoinPnL], ledger_by_cat: Dict[str, float]) -> str:
    lines = [
        "## PnL attribution (USD)",
        "",
        "| Coin | Fills | Maker % | Volume | Realized | Unrealized | Fees paid | Rebates | Net |",
        "|------|------:|--------:|-------:|---------:|-----------:|----------:|--------:|----:|",
    ]
    tot = CoinPnL(coin="TOTAL")
    for c in sorted(pnl.values(), key=lambda c: -abs(c.net)):
        maker_pct = 100.0 * c.n_maker / c.n_fills if c.n_fills else 0.0
        lines.append(
            f"| {c.coin} | {c.n_fills} | {maker_pct:.0f}% | {c.volume_usd:,.0f} "
            f"| {_fmt_usd(c.realized)} | {_fmt_usd(c.unrealized)} "
            f"| {c.fees_paid:,.2f} | {c.rebates:,.2f} | **{_fmt_usd(c.net)}** |"
        )
        tot.n_fills += c.n_fills
        tot.n_maker += c.n_maker
        tot.volume_usd += c.volume_usd
        tot.realized += c.realized
        tot.unrealized += c.unrealized
        tot.fees_paid += c.fees_paid
        tot.rebates += c.rebates
    maker_pct = 100.0 * tot.n_maker / tot.n_fills if tot.n_fills else 0.0
    lines.append(
        f"| **TOTAL** | {tot.n_fills} | {maker_pct:.0f}% | {tot.volume_usd:,.0f} "
        f"| {_fmt_usd(tot.realized)} | {_fmt_usd(tot.unrealized)} "
        f"| {tot.fees_paid:,.2f} | {tot.rebates:,.2f} | **{_fmt_usd(tot.net)}** |"
    )

    gas = ledger_by_cat.get("gas", 0.0) + ledger_by_cat.get("bridge_fee", 0.0)
    lines += [
        "",
        f"- Trading net (spread + inventory − fees): **{_fmt_usd(tot.net)}**",
        f"- Gas + bridge costs (ledger): **{_fmt_usd(gas)}**",
        f"- **All-in: {_fmt_usd(tot.net + gas)}**",
        "",
        "Reading: `Realized` on a near-flat book ≈ spread capture; `Unrealized` is",
        "inventory risk you're still carrying. If Realized is positive but Net is not,",
        "fees or inventory drift ate the edge.",
    ]
    return "\n".join(lines)


def markout_section(rows: Sequence[MarkoutRow]) -> str:
    lines = [
        "## Markouts (adverse selection)",
        "",
        "| Coin | Role | Horizon | Fills | Avg bps | Total USD | Missing |",
        "|------|------|--------:|------:|--------:|----------:|--------:|",
    ]
    for r in rows:
        role = "maker" if r.maker else "taker"
        lines.append(
            f"| {r.coin} | {role} | {r.horizon_s}s | {r.n} "
            f"| {r.avg_bps:+.2f} | {_fmt_usd(r.sum_usd)} | {r.n_missing} |"
        )
    lines += [
        "",
        "Reading: negative maker markouts mean the market moves against you after",
        "your quotes fill — informed flow is picking you off. If avg maker markout",
        "(bps) is more negative than your half-spread, widen or requote faster.",
    ]
    return "\n".join(lines)


def ledger_section(rows: Sequence[sqlite3.Row]) -> str:
    lines = [
        "## Fund-flow ledger (traceability)",
        "",
        "| When (UTC) | Chain | Category | Amount USD | Tx | Note |",
        "|------------|-------|----------|-----------:|----|------|",
    ]
    total = 0.0
    for r in rows:
        tx = (r["tx_hash"] or "")[:14] + ("…" if r["tx_hash"] and len(r["tx_hash"]) > 14 else "")
        lines.append(
            f"| {r['ts']} | {r['chain']} | {r['category']} | {_fmt_usd(r['amount_usd'])} "
            f"| `{tx}` | {r['note'] or ''} |"
        )
        total += r["amount_usd"]
    lines.append(f"\nNet ledger flow: **{_fmt_usd(total)}** (deposits should offset withdrawals; residual = costs)")
    return "\n".join(lines)


def lp_section(rows: Sequence[sqlite3.Row]) -> str:
    if not rows:
        return "## LP leg (passive)\n\n_No LP snapshots recorded yet — `mm lp add` after checking the position._"
    lines = [
        "## LP leg (passive)",
        "",
        "| When (UTC) | Venue | Pool | Value USD | Fees earned | Range | In range |",
        "|------------|-------|------|----------:|------------:|-------|----------|",
    ]
    for r in rows:
        rng = ""
        if r["range_lo"] is not None and r["range_hi"] is not None:
            rng = f"{r['range_lo']:g}–{r['range_hi']:g}"
        inr = "" if r["in_range"] is None else ("yes" if r["in_range"] else "**NO**")
        fees = f"{r['fees_earned_usd']:,.2f}" if r["fees_earned_usd"] is not None else ""
        lines.append(
            f"| {r['ts']} | {r['venue']} | {r['pool']} | {r['value_usd']:,.2f} "
            f"| {fees} | {rng} | {inr} |"
        )
    lines += [
        "",
        "Reading: (last value − first value − lp_add + lp_remove) + fees = LP PnL.",
        "Compare fee income against value drift (impermanent loss) for the talk.",
    ]
    return "\n".join(lines)


def fills_summary(fills: Sequence[sqlite3.Row]) -> str:
    if not fills:
        return "## Fills\n\n_No fills synced yet — `mm sync` once you're trading._"
    first, last = fills[0], fills[-1]
    return "\n".join(
        [
            "## Fills",
            "",
            f"- {len(fills)} fills from {_ts(first['time_ms'])} to {_ts(last['time_ms'])} UTC",
            f"- Coins: {', '.join(sorted({f['coin'] for f in fills}))}",
        ]
    )


def build_report(
    fills: Sequence[sqlite3.Row],
    pnl: Dict[str, CoinPnL],
    markouts: Sequence[MarkoutRow],
    ledger_rows: Sequence[sqlite3.Row],
    ledger_by_cat: Dict[str, float],
    lp_rows: Sequence[sqlite3.Row],
    address: Optional[str],
    positions_note: str = "",
) -> str:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts: List[str] = [f"# MM competition report — {now}", ""]
    if address:
        parts.append(f"Account: `{address}`\n")
    parts.append(fills_summary(fills))
    parts.append("")
    if pnl:
        parts.append(pnl_section(pnl, ledger_by_cat))
        parts.append("")
    if markouts:
        parts.append(markout_section(markouts))
        parts.append("")
    if positions_note:
        parts.append(positions_note)
        parts.append("")
    parts.append(lp_section(lp_rows))
    parts.append("")
    parts.append(ledger_section(ledger_rows) if ledger_rows else
                 "## Fund-flow ledger (traceability)\n\n_Empty — log every bridge/gas/deposit with `mm ledger add`._")
    return "\n".join(parts) + "\n"
