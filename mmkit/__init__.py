"""mmkit — market-making competition tracking toolkit.

Zero-dependency (stdlib only) tooling for a week-long MM competition:
  - sync fills from Hyperliquid's public info API
  - markout analysis (adverse-selection measurement)
  - PnL attribution: spread capture / fees & rebates / inventory / gas
  - fund-flow ledger for cross-chain traceability
  - LP position snapshots for the passive (AMM) leg
  - daily markdown reports for the end-of-week presentation
"""

__version__ = "0.1.0"
