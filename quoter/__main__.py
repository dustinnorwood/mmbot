"""Quoter CLI.

    .venv/bin/python -m quoter run     --config quoter.json [--mainnet] [--dry-run]
    .venv/bin/python -m quoter status  --config quoter.json [--mainnet]
    .venv/bin/python -m quoter flatten --config quoter.json [--mainnet]

Testnet is the default everywhere; --mainnet is a deliberate act.
"""

import argparse
import logging

from .agent import cmd_agent_address, cmd_approve_agent
from .config import QuoterConfig
from .live import Quoter, flatten, status


def main():
    p = argparse.ArgumentParser(prog="quoter")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "status", "flatten"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="quoter.json")
        sp.add_argument("--mainnet", action="store_true",
                        help="trade real funds (default is TESTNET)")
        if name == "run":
            sp.add_argument("--dry-run", action="store_true",
                            help="compute and print quotes without sending")

    sp = sub.add_parser("agent-address", help="print the agent wallet's address")
    sp = sub.add_parser("approve-agent",
                        help="approve the agent wallet for trading (one-time per network)")
    sp.add_argument("--mainnet", action="store_true")
    sp.add_argument("--name", default="mm-quoter", help="agent name shown in the app")
    sp.add_argument("--agent-address", default=None,
                    help="agent address to approve (skips reading the local key file)")

    args = p.parse_args()
    if args.cmd == "agent-address":
        return cmd_agent_address()
    if args.cmd == "approve-agent":
        return cmd_approve_agent(args.mainnet, args.name, args.agent_address)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = QuoterConfig.load(args.config)

    if args.cmd == "run":
        if args.mainnet and not args.dry_run:
            print(f"⚠️  MAINNET quoting on {cfg.coin}: real funds, max position "
                  f"${cfg.max_position_usd:.0f}, halt at -${cfg.max_daily_loss_usd:.0f}.")
            if input("type 'yes' to continue: ").strip().lower() != "yes":
                raise SystemExit("aborted")
        Quoter(cfg, mainnet=args.mainnet, dry_run=args.dry_run).run()
    elif args.cmd == "status":
        status(cfg, args.mainnet)
    elif args.cmd == "flatten":
        flatten(cfg, args.mainnet)


if __name__ == "__main__":
    main()
