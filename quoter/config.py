"""Quoter configuration: JSON file + key loading.

The agent PRIVATE KEY never lives in this repo. It is read from
~/.mmkit/hl_agent_key (override with env MMKIT_KEY_FILE), which should be
chmod 600. The key must be an AGENT (API) wallet approved by your master
account — an agent key can trade but can never withdraw funds.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .engine import QuoteParams

DEFAULT_KEY_FILE = Path.home() / ".mmkit" / "hl_agent_key"


@dataclass
class QuoterConfig:
    account_address: str          # MASTER account address (the one holding funds)
    coin: str
    n_levels: int
    level_notional_usd: float
    base_half_spread_bps: float
    level_step_bps: float
    vol_k: float
    skew_gain_bps: float
    max_position_usd: float
    requote_bps: float
    max_quote_age_s: float
    max_daily_loss_usd: float
    poll_interval_s: float

    @classmethod
    def load(cls, path: str) -> "QuoterConfig":
        raw = json.loads(Path(path).read_text())
        try:
            return cls(**raw)
        except TypeError as e:
            sys.exit(f"config error in {path}: {e}")

    def quote_params(self, sz_decimals: int) -> QuoteParams:
        return QuoteParams(
            coin=self.coin,
            sz_decimals=sz_decimals,
            n_levels=self.n_levels,
            level_notional_usd=self.level_notional_usd,
            base_half_spread_bps=self.base_half_spread_bps,
            level_step_bps=self.level_step_bps,
            vol_k=self.vol_k,
            skew_gain_bps=self.skew_gain_bps,
            max_position_usd=self.max_position_usd,
            requote_bps=self.requote_bps,
            max_quote_age_s=self.max_quote_age_s,
        )


def load_agent_key() -> str:
    """Read the agent private key, refusing world/group-readable files."""
    path = Path(os.environ.get("MMKIT_KEY_FILE", DEFAULT_KEY_FILE))
    if not path.exists():
        sys.exit(
            f"agent key not found at {path}\n"
            "Create it (never the master key — an approved agent/API wallet key):\n"
            "  mkdir -p ~/.mmkit && chmod 700 ~/.mmkit\n"
            "  read -rs KEY && echo \"$KEY\" > ~/.mmkit/hl_agent_key && "
            "chmod 600 ~/.mmkit/hl_agent_key && unset KEY"
        )
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        sys.exit(f"refusing to use {path}: it is group/world-readable. Run: chmod 600 {path}")
    key = path.read_text().strip()
    if key.startswith("0x"):
        key = key[2:]
    if len(key) != 64:
        sys.exit(f"{path} does not look like a 32-byte hex private key")
    return "0x" + key
