"""Pure strategy math for the quoter. No SDK imports — everything here is
deterministic and unit-tested.

Strategy: symmetric ladder around an inventory-skewed reservation price,
with volatility-scaled half-spread.

    inv_ratio   = clamp(position_usd / max_position_usd, -1, +1)
    half_bps    = max(base_half_spread_bps, vol_k * sigma_1m_bps)
    reservation = mid * (1 - skew_gain_bps * inv_ratio / 1e4)
    bid_i       = reservation * (1 - (half_bps + i*step_bps)/1e4)
    ask_i       = reservation * (1 + (half_bps + i*step_bps)/1e4)

Long inventory shifts both sides DOWN (ask more likely to fill, bid less),
pushing the book back toward flat. At |inv_ratio| >= 1 the increasing side
is suppressed entirely (reduce-only quoting).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional


# ------------------------------------------------------------- vol estimate
class VolEstimator:
    """EWMA realized vol from irregularly-sampled mids.

    Tracks variance per second of log returns; exposes sigma over 1 minute
    in bps, which is the natural unit for spread setting.
    """

    def __init__(self, halflife_s: float = 300.0):
        self.halflife_s = halflife_s
        self._var_per_s: Optional[float] = None  # EWMA of r^2/dt
        self._last_mid: Optional[float] = None
        self._last_t: Optional[float] = None

    def update(self, mid: float, t: float) -> None:
        if self._last_mid is not None and self._last_t is not None:
            dt = t - self._last_t
            if dt > 0:
                r = math.log(mid / self._last_mid)
                inst_var = (r * r) / dt
                alpha = 1 - 0.5 ** (dt / self.halflife_s)
                if self._var_per_s is None:
                    self._var_per_s = inst_var
                else:
                    self._var_per_s += alpha * (inst_var - self._var_per_s)
        self._last_mid = mid
        self._last_t = t

    def sigma_1m_bps(self) -> Optional[float]:
        if self._var_per_s is None:
            return None
        return math.sqrt(self._var_per_s * 60.0) * 1e4


# ------------------------------------------------------------- rounding
def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid tick rules for perps: <=5 significant figures AND
    <=(6 - szDecimals) decimal places. Integer prices are always allowed."""
    px = float(f"{px:.5g}")
    return round(px, max(0, 6 - sz_decimals))


def round_sz(sz: float, sz_decimals: int) -> float:
    factor = 10 ** sz_decimals
    return math.floor(sz * factor) / factor


def size_for_notional(notional_usd: float, px: float, sz_decimals: int,
                      min_notional_usd: float = 10.0) -> float:
    """Coin size for a target USD notional, respecting szDecimals and the
    venue's $10 minimum order value (with a small buffer for price drift)."""
    sz = round_sz(notional_usd / px, sz_decimals)
    step = 10 ** -sz_decimals
    # bump up until we clear the minimum notional with 5% headroom
    while sz * px < min_notional_usd * 1.05:
        sz = round(sz + step, sz_decimals)
    return sz


# ------------------------------------------------------------- quotes
@dataclass
class QuoteParams:
    coin: str
    sz_decimals: int
    n_levels: int = 3
    level_notional_usd: float = 15.0
    base_half_spread_bps: float = 6.0
    level_step_bps: float = 3.0
    vol_k: float = 1.5              # half-spread = max(base, vol_k * sigma_1m)
    skew_gain_bps: float = 4.0      # reservation shift at full inventory
    max_position_usd: float = 120.0
    requote_bps: float = 2.0        # requote when mid drifts this far
    max_quote_age_s: float = 45.0   # freshness refresh (cheap, occasional)
    min_notional_usd: float = 10.0


@dataclass
class Quote:
    is_buy: bool
    px: float
    sz: float

    @property
    def notional(self) -> float:
        return self.px * self.sz


def desired_quotes(mid: float, position_usd: float, p: QuoteParams,
                   sigma_1m_bps: Optional[float]) -> List[Quote]:
    inv_ratio = max(-1.0, min(1.0, position_usd / p.max_position_usd))
    half_bps = p.base_half_spread_bps
    if sigma_1m_bps is not None:
        half_bps = max(half_bps, p.vol_k * sigma_1m_bps)
    reservation = mid * (1 - p.skew_gain_bps * inv_ratio / 1e4)

    quotes: List[Quote] = []
    for i in range(p.n_levels):
        off = (half_bps + i * p.level_step_bps) / 1e4
        bid_px = reservation * (1 - off)
        ask_px = reservation * (1 + off)
        # never let skew push a quote through the mid (ALO would reject anyway)
        bid_px = min(bid_px, mid * (1 - 0.5 / 1e4))
        ask_px = max(ask_px, mid * (1 + 0.5 / 1e4))
        bid_px = round_px(bid_px, p.sz_decimals)
        ask_px = round_px(ask_px, p.sz_decimals)
        if inv_ratio < 1.0:   # room to buy
            quotes.append(Quote(True, bid_px, size_for_notional(
                p.level_notional_usd, bid_px, p.sz_decimals, p.min_notional_usd)))
        if inv_ratio > -1.0:  # room to sell
            quotes.append(Quote(False, ask_px, size_for_notional(
                p.level_notional_usd, ask_px, p.sz_decimals, p.min_notional_usd)))
    return quotes


def should_requote(now: float, last_quote_t: Optional[float],
                   ref_mid: Optional[float], mid: float,
                   position_changed: bool, p: QuoteParams) -> bool:
    if last_quote_t is None or ref_mid is None:
        return True
    if position_changed:
        return True
    if abs(mid - ref_mid) / ref_mid * 1e4 >= p.requote_bps:
        return True
    if now - last_quote_t >= p.max_quote_age_s:
        return True
    return False


# ------------------------------------------------------------- safety
@dataclass
class SafetyState:
    session_start_equity: Optional[float] = None
    halted: bool = False
    halt_reason: str = ""


def check_halt(state: SafetyState, equity: float, max_daily_loss_usd: float,
               kill_file_exists: bool) -> SafetyState:
    if state.session_start_equity is None:
        state.session_start_equity = equity
    if kill_file_exists:
        state.halted = True
        state.halt_reason = "kill file present (STOP)"
    elif equity < state.session_start_equity - max_daily_loss_usd:
        state.halted = True
        state.halt_reason = (
            f"max daily loss hit: equity {equity:.2f} < "
            f"{state.session_start_equity:.2f} - {max_daily_loss_usd:.2f}"
        )
    return state
