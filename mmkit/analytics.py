"""Analytics: PnL attribution and markout (adverse-selection) measurement.

PnL decomposition per coin, in USD:

    total = realized (avg-cost round trips)   <- "spread capture" when flat-ish
          + unrealized (open inventory vs mark)
          - fees paid (+ rebates earned)

Ledger costs (gas, bridge fees) are layered on top in the report, so the
final number is: what did the whole operation earn after every cost.

Markout: for each fill, how did the mid move against us h seconds later?
    markout_usd(h) = dir * (mid(t+h) - fill_px) * sz      dir = +1 buy, -1 sell
    markout_bps(h) = dir * (mid(t+h) - fill_px) / fill_px * 10_000
Consistently negative markouts = adverse selection: your quotes are being
picked off by better-informed flow, and your spread is too tight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence


@dataclass
class CoinPnL:
    coin: str
    volume_usd: float = 0.0
    n_fills: int = 0
    n_maker: int = 0
    realized: float = 0.0          # avg-cost realized pnl (pre-fee)
    venue_realized: float = 0.0    # venue-reported closedPnl sum (cross-check)
    fees_paid: float = 0.0         # positive fees only
    rebates: float = 0.0           # negative fees, stored as positive number
    position: float = 0.0          # signed inventory after last fill
    avg_cost: float = 0.0
    mark: Optional[float] = None
    unrealized: float = 0.0

    @property
    def net(self) -> float:
        return self.realized + self.unrealized - self.fees_paid + self.rebates


def attribute_pnl(
    fills: Sequence[Mapping[str, Any]],
    marks: Optional[Mapping[str, float]] = None,
) -> Dict[str, CoinPnL]:
    """Average-cost PnL attribution per coin.

    `fills` must be in chronological order with keys:
        coin, side ('B'/'A'), px, sz, fee, crossed, closed_pnl
    `marks` maps coin -> current mid, for unrealized PnL on open inventory.
    """
    out: Dict[str, CoinPnL] = {}
    for f in fills:
        coin = f["coin"]
        c = out.setdefault(coin, CoinPnL(coin=coin))
        px, sz = float(f["px"]), float(f["sz"])
        signed = sz if f["side"] == "B" else -sz

        c.n_fills += 1
        c.volume_usd += px * sz
        if not f["crossed"]:
            c.n_maker += 1
        fee = float(f["fee"] or 0)
        if fee >= 0:
            c.fees_paid += fee
        else:
            c.rebates += -fee
        c.venue_realized += float(f["closed_pnl"] or 0)

        pos, avg = c.position, c.avg_cost
        if pos == 0 or (pos > 0) == (signed > 0):
            # extending (or opening) in the same direction: new average cost
            new_pos = pos + signed
            c.avg_cost = (abs(pos) * avg + abs(signed) * px) / abs(new_pos)
            c.position = new_pos
        else:
            # reducing / flipping: realize on the closed quantity
            closed = min(abs(pos), abs(signed))
            direction = 1.0 if pos > 0 else -1.0
            c.realized += (px - avg) * closed * direction
            remainder = abs(signed) - abs(pos)
            if remainder > 1e-12:
                # flipped through zero: remainder opens a new position at px
                c.position = remainder * (1.0 if signed > 0 else -1.0)
                c.avg_cost = px
            else:
                c.position = pos + signed
                if abs(c.position) < 1e-12:
                    c.position = 0.0
                    c.avg_cost = 0.0

    if marks:
        for coin, c in out.items():
            mark = marks.get(coin)
            if mark is not None:
                c.mark = mark
                c.unrealized = (mark - c.avg_cost) * c.position
    return out


# ------------------------------------------------------------------ markout
@dataclass
class MarkoutRow:
    coin: str
    maker: bool
    horizon_s: int
    n: int = 0
    sum_usd: float = 0.0
    sum_bps: float = 0.0
    n_missing: int = 0

    @property
    def avg_bps(self) -> float:
        return self.sum_bps / self.n if self.n else 0.0


def compute_markouts(
    fills: Sequence[Mapping[str, Any]],
    mid_at: Callable[[str, int], Optional[float]],
    horizons_s: Sequence[int] = (60, 300),
) -> List[MarkoutRow]:
    """Aggregate markouts by (coin, maker/taker, horizon).

    `mid_at(coin, t_ms)` returns the mid/close price at time t_ms, or None
    if unavailable (e.g. horizon extends past now) — those fills are counted
    in n_missing rather than silently dropped.
    """
    table: Dict[tuple, MarkoutRow] = {}
    for f in fills:
        coin = f["coin"]
        maker = not f["crossed"]
        px, sz = float(f["px"]), float(f["sz"])
        direction = 1.0 if f["side"] == "B" else -1.0
        t = int(f["time_ms"])
        for h in horizons_s:
            key = (coin, maker, h)
            row = table.setdefault(key, MarkoutRow(coin=coin, maker=maker, horizon_s=h))
            mid = mid_at(coin, t + h * 1000)
            if mid is None:
                row.n_missing += 1
                continue
            row.n += 1
            row.sum_usd += direction * (mid - px) * sz
            row.sum_bps += direction * (mid - px) / px * 10_000
    return sorted(table.values(), key=lambda r: (r.coin, not r.maker, r.horizon_s))
