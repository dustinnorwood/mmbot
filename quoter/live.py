"""Live quoting loop against Hyperliquid.

Safety posture:
  - TESTNET by default; mainnet requires an explicit --mainnet flag.
  - --dry-run computes and prints quotes without sending anything.
  - All orders are post-only (ALO): we can never accidentally pay taker.
  - Hard halt on: kill file `STOP` in the working directory, or equity
    dropping more than max_daily_loss_usd below session start.
  - On any exit (including Ctrl-C), all resting orders are cancelled.
    Positions are NOT auto-closed — use `flatten` for that.

Rate-limit posture: one allMids + one user_state poll per loop (IP-weighted,
cheap); order/cancel actions only fire on requote triggers (mid drift beyond
threshold, a fill, or staleness), not on a timer.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from .config import QuoterConfig, load_agent_key
from .engine import (Quote, SafetyState, VolEstimator, check_halt,
                     desired_quotes, should_requote)

log = logging.getLogger("quoter")

KILL_FILE = Path("STOP")


class Quoter:
    def __init__(self, cfg: QuoterConfig, mainnet: bool, dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run
        self.base_url = constants.MAINNET_API_URL if mainnet else constants.TESTNET_API_URL
        self.info = Info(self.base_url, skip_ws=True)

        self.exchange: Optional[Exchange] = None
        if not dry_run:
            wallet = eth_account.Account.from_key(load_agent_key())
            log.info("agent wallet: %s (signs for master %s)", wallet.address, cfg.account_address)
            self.exchange = Exchange(
                wallet, self.base_url, account_address=cfg.account_address
            )

        meta = self.info.meta()
        sz_decimals = None
        for asset in meta["universe"]:
            if asset["name"] == cfg.coin:
                sz_decimals = asset["szDecimals"]
                break
        if sz_decimals is None:
            raise SystemExit(f"coin {cfg.coin!r} not found in perp universe on {self.base_url}")
        self.params = cfg.quote_params(sz_decimals)

        self.vol = VolEstimator()
        self.safety = SafetyState()
        self.last_quote_t: Optional[float] = None
        self.ref_mid: Optional[float] = None
        self.last_position: Optional[float] = None

    # ---------------------------------------------------------------- state
    def read_state(self) -> Dict:
        mids = self.info.all_mids()
        mid = float(mids[self.cfg.coin])
        us = self.info.user_state(self.cfg.account_address)
        equity = float(us["marginSummary"]["accountValue"])
        szi = 0.0
        for ap in us.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == self.cfg.coin:
                szi = float(pos.get("szi", 0))
        return {"mid": mid, "equity": equity, "szi": szi}

    def open_oids(self) -> List[int]:
        orders = self.info.open_orders(self.cfg.account_address)
        return [o["oid"] for o in orders if o.get("coin") == self.cfg.coin]

    # ---------------------------------------------------------------- orders
    def cancel_all(self) -> None:
        oids = self.open_oids()
        if not oids:
            return
        if self.dry_run:
            log.info("[dry-run] would cancel %d orders", len(oids))
            return
        resp = self.exchange.bulk_cancel(
            [{"coin": self.cfg.coin, "oid": oid} for oid in oids]
        )
        log.info("cancelled %d orders (status=%s)", len(oids), resp.get("status"))

    def place(self, quotes: List[Quote]) -> None:
        if self.dry_run:
            for q in quotes:
                log.info("[dry-run] %s %s %g @ %g  ($%.2f)",
                         "BUY " if q.is_buy else "SELL", self.cfg.coin, q.sz, q.px, q.notional)
            return
        reqs = [
            {
                "coin": self.cfg.coin,
                "is_buy": q.is_buy,
                "sz": q.sz,
                "limit_px": q.px,
                "order_type": {"limit": {"tif": "Alo"}},
                "reduce_only": False,
            }
            for q in quotes
        ]
        resp = self.exchange.bulk_orders(reqs)
        statuses = (resp.get("response", {}).get("data", {}).get("statuses", [])
                    if isinstance(resp, dict) else [])
        n_ok = sum(1 for s in statuses if "resting" in s)
        errs = [s["error"] for s in statuses if "error" in s]
        log.info("placed %d/%d quotes", n_ok, len(quotes))
        for e in errs:
            # ALO rejects ("would cross") land here — expected on fast moves
            log.warning("order rejected: %s", e)

    # ---------------------------------------------------------------- loop
    def run(self) -> None:
        mode = "DRY-RUN" if self.dry_run else ("MAINNET" if "testnet" not in self.base_url else "TESTNET")
        log.info("starting quoter on %s [%s] — kill switch: create a file named STOP", self.cfg.coin, mode)
        try:
            while True:
                t = time.time()
                st = self.read_state()
                self.vol.update(st["mid"], t)

                self.safety = check_halt(
                    self.safety, st["equity"], self.cfg.max_daily_loss_usd, KILL_FILE.exists()
                )
                if self.safety.halted:
                    log.error("HALT: %s — cancelling all orders", self.safety.halt_reason)
                    self.cancel_all()
                    log.error("halted. position is UNCHANGED — run `flatten` to close it.")
                    return

                position_usd = st["szi"] * st["mid"]
                position_changed = (
                    self.last_position is not None and abs(st["szi"] - self.last_position) > 1e-12
                )
                if position_changed:
                    log.info("FILL detected: position %+.6f -> %+.6f (%.2f USD)",
                             self.last_position, st["szi"], position_usd)
                self.last_position = st["szi"]

                if should_requote(t, self.last_quote_t, self.ref_mid, st["mid"],
                                  position_changed, self.params):
                    sigma = self.vol.sigma_1m_bps()
                    quotes = desired_quotes(st["mid"], position_usd, self.params, sigma)
                    log.info("requote: mid=%g pos=%.2f$ sigma1m=%s inv_ratio=%.2f -> %d quotes",
                             st["mid"], position_usd,
                             f"{sigma:.1f}bps" if sigma else "n/a",
                             max(-1, min(1, position_usd / self.params.max_position_usd)),
                             len(quotes))
                    self.cancel_all()
                    self.place(quotes)
                    self.last_quote_t = t
                    self.ref_mid = st["mid"]

                time.sleep(self.cfg.poll_interval_s)
        except KeyboardInterrupt:
            log.info("interrupt — cancelling all resting orders")
            self.cancel_all()
            log.info("done. position is UNCHANGED — run `flatten` if you want to close it.")


def flatten(cfg: QuoterConfig, mainnet: bool) -> None:
    """Cancel everything on the coin, then close the position with a
    slippage-capped market order (SDK market_close is reduce-only by design)."""
    base_url = constants.MAINNET_API_URL if mainnet else constants.TESTNET_API_URL
    wallet = eth_account.Account.from_key(load_agent_key())
    info = Info(base_url, skip_ws=True)
    exchange = Exchange(wallet, base_url, account_address=cfg.account_address)

    oids = [o["oid"] for o in info.open_orders(cfg.account_address) if o.get("coin") == cfg.coin]
    if oids:
        exchange.bulk_cancel([{"coin": cfg.coin, "oid": oid} for oid in oids])
        print(f"cancelled {len(oids)} resting orders on {cfg.coin}")

    us = info.user_state(cfg.account_address)
    szi = 0.0
    for ap in us.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == cfg.coin:
            szi = float(pos.get("szi", 0))
    if szi == 0:
        print(f"{cfg.coin}: already flat ✓")
        return
    print(f"{cfg.coin}: closing position {szi:+g} …")
    resp = exchange.market_close(cfg.coin)
    print(f"market_close status: {resp.get('status') if isinstance(resp, dict) else resp}")


def status(cfg: QuoterConfig, mainnet: bool) -> None:
    base_url = constants.MAINNET_API_URL if mainnet else constants.TESTNET_API_URL
    info = Info(base_url, skip_ws=True)
    us = info.user_state(cfg.account_address)
    ms = us["marginSummary"]
    print(f"equity:       {float(ms['accountValue']):,.2f} USDC")
    print(f"withdrawable: {float(us.get('withdrawable', 0)):,.2f} USDC")
    poss = [ap["position"] for ap in us.get("assetPositions", [])
            if float(ap.get("position", {}).get("szi", 0)) != 0]
    if poss:
        for p in poss:
            print(f"position:     {p['coin']} {float(p['szi']):+g} @ entry {p.get('entryPx')}"
                  f"  uPnL {float(p.get('unrealizedPnl', 0)):+.2f}")
    else:
        print("position:     FLAT ✓")
    orders = info.open_orders(cfg.account_address)
    print(f"open orders:  {len(orders)}")
    for o in orders:
        side = "BUY " if o.get("side") == "B" else "SELL"
        print(f"  {o.get('coin'):<10} {side} {o.get('sz')} @ {o.get('limitPx')}  (oid {o.get('oid')})")
