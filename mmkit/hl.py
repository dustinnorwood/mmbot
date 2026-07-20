"""Read-only client for Hyperliquid's public info API.

Everything here is unauthenticated: the info endpoint serves public data
keyed by wallet address, so no keys ever touch this tool. Endpoints are
overridable via env vars in case the API host changes:

    HL_API_URL          (default https://api.hyperliquid.xyz)
    HL_TESTNET_API_URL  (default https://api.hyperliquid-testnet.xyz)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

MAINNET_URL = os.environ.get("HL_API_URL", "https://api.hyperliquid.xyz")
TESTNET_URL = os.environ.get("HL_TESTNET_API_URL", "https://api.hyperliquid-testnet.xyz")

# The API caps userFillsByTime responses; page until we get fewer than this.
FILLS_PAGE_LIMIT = 2000


class HLError(RuntimeError):
    pass


class HLClient:
    def __init__(self, testnet: bool = False, timeout: float = 15.0):
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self.timeout = timeout

    def _post(self, payload: Dict[str, Any], retries: int = 3) -> Any:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base_url + "/info",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                # 429 = rate limited: back off and retry. 4xx otherwise = our bug.
                if e.code == 429 and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    last_err = e
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise HLError(f"info API HTTP {e.code} for {payload.get('type')}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
        raise HLError(f"info API unreachable after {retries} tries: {last_err}")

    # ------------------------------------------------------------------
    def all_mids(self) -> Dict[str, float]:
        raw = self._post({"type": "allMids"})
        return {coin: float(px) for coin, px in raw.items()}

    def user_fills_by_time(self, address: str, start_ms: int, end_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        """All fills for `address` in [start_ms, end_ms], paginating past the response cap."""
        out: List[Dict[str, Any]] = []
        cursor = start_ms
        end = end_ms or int(time.time() * 1000)
        while True:
            page = self._post(
                {"type": "userFillsByTime", "user": address, "startTime": cursor, "endTime": end}
            )
            if not isinstance(page, list):
                raise HLError(f"unexpected userFillsByTime response: {type(page)}")
            page.sort(key=lambda f: (f.get("time", 0), f.get("tid", 0)))
            out.extend(page)
            if len(page) < FILLS_PAGE_LIMIT:
                break
            cursor = int(page[-1]["time"]) + 1  # next page starts after last fill seen
        # de-dupe on tid in case of overlap
        seen = set()
        deduped = []
        for f in out:
            tid = f.get("tid")
            if tid in seen:
                continue
            seen.add(tid)
            deduped.append(f)
        return deduped

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
        return self._post(
            {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
            }
        )

    def clearinghouse_state(self, address: str) -> Dict[str, Any]:
        return self._post({"type": "clearinghouseState", "user": address})

    def spot_clearinghouse_state(self, address: str) -> Dict[str, Any]:
        return self._post({"type": "spotClearinghouseState", "user": address})
