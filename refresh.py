"""Hands-off refresh: pull live Yahoo data and save data/live_snapshot.json.

Run it however you like — cron, GitHub Actions (see .github/workflows/refresh.yml),
or `python refresh.py`. No Streamlit, no keys, no paid APIs.
Exit 0 even if some tickers fail; the snapshot records per-ticker errors.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from live_data import build_snapshot, load_watchlist, merge_big_contracts, save_snapshot


def main() -> int:
    tickers = load_watchlist()
    snap = build_snapshot(tickers)
    save_snapshot(snap)
    big = merge_big_contracts(snap)
    ok = snap["tickers_ok"]
    print(f"Refreshed {snap['fetched_at']}: {ok}/{snap['tickers_scanned']} tickers OK, "
          f"{len(snap['contracts'])} unusual contracts, "
          f"${snap['total_call_premium']:,.0f} call / ${snap['total_put_premium']:,.0f} put premium. "
          f"Big-contracts log: {len(big)} tracked.")
    if snap.get("errors"):
        print("Errors:", snap["errors"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
