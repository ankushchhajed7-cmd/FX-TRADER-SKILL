"""fetch_data.py v1.00 — downloads 5-minute candles from Yahoo for every pair
and merges them into data/5m/<PAIR>/YYYY-MM.csv (UTC).

Runs inside the GitHub Action (fetch-data.yml). Yahoo keeps only the last
60 days of 5m bars, so running it at least once a month keeps history growing
without gaps. Standard library only.

Usage:  python3 tools/fetch_data.py [--data data] [--pairs XAUUSD,EURUSD]
"""

import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fx-core"))
import fxcore  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
    ap.add_argument("--pairs", default=",".join(fxcore.PAIRS))
    ap.add_argument("--range", default="60d")
    a = ap.parse_args()

    failed, lines = 0, []
    for pair in [p.strip().upper() for p in a.pairs.split(",") if p.strip()]:
        try:
            bars = fxcore.fetch_yahoo(pair, "5m", a.range)
        except Exception as e:  # network, Yahoo format change, ...
            failed += 1
            lines.append(f"{pair}: FAILED ({e.__class__.__name__}: {e})")
            continue
        added = fxcore.write_month_files(a.data, pair, bars)
        last = fxcore.iso(bars[-1][0]) if bars else "-"
        lines.append(f"{pair}: {len(bars)} bars fetched, {added} new, last {last} UTC")
        time.sleep(1)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    os.makedirs(a.data, exist_ok=True)
    with open(os.path.join(a.data, "last_update.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"Updated {stamp}\n" + "\n".join(lines) + "\n")
    print("\n".join(lines))
    # fail the job only if every pair failed
    return 1 if failed == len(lines) else 0


if __name__ == "__main__":
    sys.exit(main())
