"""Read-only client for Lighter's public REST API (zk-rollup perp DEX).

Verified endpoints (2026-07-20): account state/positions and candles need NO
credentials. Own-fill history via:
  - /api/v1/trades with an auth token (set LIGHTER_AUTH_TOKEN; a read-only
    `ro:` token minted via lighter-sdk is enough and cannot trade/withdraw), or
  - the no-auth fallback: /api/v1/recentTrades per market, filtered by our
    account index client-side. Window is the last `limit` (<=100) trades per
    market, so sync every few hours in active markets.

Env overrides:
    LIGHTER_API_URL          (default https://mainnet.zklighter.elliot.ai)
    LIGHTER_TESTNET_API_URL  (default https://testnet.zklighter.elliot.ai)
    LIGHTER_AUTH_TOKEN       (optional, enables the reliable /trades path)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

MAINNET_URL = os.environ.get("LIGHTER_API_URL", "https://mainnet.zklighter.elliot.ai")
TESTNET_URL = os.environ.get("LIGHTER_TESTNET_API_URL", "https://testnet.zklighter.elliot.ai")

VENUE = "lighter"


class LighterError(RuntimeError):
    pass


class LighterClient:
    def __init__(self, testnet: bool = False, timeout: float = 15.0):
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self.timeout = timeout
        self.auth_token = os.environ.get("LIGHTER_AUTH_TOKEN")
        self._markets: Optional[Dict[str, Dict[str, Any]]] = None  # symbol -> details

    def _get(self, path: str, params: Dict[str, Any], auth: bool = False,
             retries: int = 3) -> Any:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}/api/v1/{path}?{qs}"
        headers = {}
        if auth:
            if not self.auth_token:
                raise LighterError(
                    f"{path} requires LIGHTER_AUTH_TOKEN (a read-only 'ro:' token works)"
                )
            headers["Authorization"] = self.auth_token
        req = urllib.request.Request(url, headers=headers)
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                    code = data.get("code")
                    if code is not None and code != 200:
                        raise LighterError(f"{path}: api code {code}: {data.get('message')}")
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    time.sleep(2.0 * (attempt + 1))  # 60 req/min IP limit
                    last_err = e
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise LighterError(f"{path} HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
        raise LighterError(f"{path} unreachable after {retries} tries: {last_err}")

    # ------------------------------------------------------------ metadata
    def markets(self) -> Dict[str, Dict[str, Any]]:
        """symbol -> order book details (market_id, mins, fees...), cached."""
        if self._markets is None:
            data = self._get("orderBookDetails", {})
            books = data.get("order_book_details", data.get("orderBookDetails", []))
            self._markets = {b["symbol"]: b for b in books}
        return self._markets

    def market_id(self, symbol: str) -> int:
        m = self.markets().get(symbol)
        if m is None:
            raise LighterError(f"market {symbol!r} not found on Lighter")
        return int(m["market_id"])

    # ------------------------------------------------------------ account
    def accounts_by_l1(self, l1_address: str) -> List[Dict[str, Any]]:
        data = self._get("accountsByL1Address", {"l1_address": l1_address})
        return data.get("sub_accounts", data.get("accounts", []))

    def account(self, account_index: int) -> Dict[str, Any]:
        data = self._get("account", {"by": "index", "value": account_index})
        accounts = data.get("accounts", [])
        if not accounts:
            raise LighterError(f"no account at index {account_index}")
        return accounts[0]

    # ------------------------------------------------------------ market data
    def candles_1m(self, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        """1m candles in [start_ms, end_ms], paginated past the 500/request cap.
        Returns dicts with keys 't' (open ms) and 'c' (close) like the HL client."""
        mid = self.market_id(symbol)
        out: List[Dict[str, Any]] = []
        cursor = start_ms
        while cursor < end_ms:
            data = self._get("candles", {
                "market_id": mid, "resolution": "1m",
                "start_timestamp": cursor, "end_timestamp": end_ms,
                "count_back": 500,
            })
            candles = data.get("c", data.get("candles", []))
            if not candles:
                break
            out.extend(candles)
            last_t = int(candles[-1]["t"])
            if last_t <= cursor:
                break
            cursor = last_t + 60_000
            if len(candles) < 500:
                break
        return out

    def latest_close(self, symbol: str) -> Optional[float]:
        now = int(time.time() * 1000)
        candles = self.candles_1m(symbol, now - 10 * 60_000, now)
        return float(candles[-1]["c"]) if candles else None

    # ------------------------------------------------------------ fills
    def recent_trades(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        data = self._get("recentTrades", {"market_id": self.market_id(symbol), "limit": limit})
        return data.get("trades", [])

    def my_trades_auth(self, account_index: int, limit: int = 100,
                      cursor: Optional[str] = None) -> Dict[str, Any]:
        """Reliable own-fill history — requires LIGHTER_AUTH_TOKEN."""
        return self._get("trades", {
            "account_index": account_index, "sort_by": "timestamp",
            "limit": limit, "cursor": cursor,
        }, auth=True)


# ---------------------------------------------------------------- mapping
def trade_to_fill(trade: Dict[str, Any], my_index: int, symbol: str) -> Optional[Dict[str, Any]]:
    """Map a Lighter trade object to our fills schema. Returns None if the
    trade doesn't involve `my_index`.

    Side: we bought if we were the bid account. Maker/taker: `is_maker_ask`
    says which side rested; we were maker iff we were on that side.
    """
    bid_acct = trade.get("bid_account_id")
    ask_acct = trade.get("ask_account_id")
    if my_index not in (bid_acct, ask_acct):
        return None
    if bid_acct == ask_acct:
        # self-match records shouldn't exist (STP), but never double-count
        return None
    i_am_bid = bid_acct == my_index
    maker_is_ask = bool(trade.get("is_maker_ask"))
    i_am_maker = (not i_am_bid) if maker_is_ask else i_am_bid
    fee = trade.get("maker_fee" if i_am_maker else "taker_fee", 0) or 0
    pnl = trade.get("bid_account_pnl" if i_am_bid else "ask_account_pnl", 0) or 0
    return {
        "tid": int(trade["trade_id"]),
        "oid": None,
        "coin": symbol,
        "side": "B" if i_am_bid else "A",
        "px": float(trade["price"]),
        "sz": float(trade["size"]),
        "time": int(trade["timestamp"]),
        "crossed": not i_am_maker,
        "fee": float(fee),
        "feeToken": "USDC",
        "closedPnl": float(pnl),
        "dir": trade.get("type"),
        "hash": trade.get("tx_hash"),
    }
