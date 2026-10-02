"""
Growth Screener
---------------
Same S&P 500 sector-by-sector scan as screener.py, but with a hard revenue
growth filter applied first: only stocks with revenue_growth >= 10% survive
to be ranked. Everything below that line is rejected outright, not just
deprioritized.

Reuses screener.py's own load_constituents / score_sector / composite
scoring unchanged (peer averages are still computed per full sector, same
as screener.py -- "vs sector peers" means vs the real sector, not just the
other growth survivors). This script only filters the results screener.py
already produces; it doesn't reimplement any fetching, clipping, or scoring
logic, so a fix in screener.py is picked up here automatically.

Note: like screener.py, a ticker only gets a composite score if it has
positive trailing EPS. A high-growth but unprofitable company will still be
excluded even if its revenue growth clears the 10% bar -- that's inherited
from screener.py's scoring logic unchanged, not a bug in the growth filter.

Run: python3 growth_screener.py            (full S&P 500 scan)
     python3 growth_screener.py --test     (screener's TEST_MODE, small
                                             ticker slice per sector, for
                                             a quick end-to-end smoke run)

Outputs: prints to terminal AND writes growth_screener_results.txt (full
list of everyone who passed the filter, not just the top 10)

Requires:
  pip install yfinance --break-system-packages
  sp500_constituents.csv in the shared ~/market-data folder
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screener"))
import screener

REVENUE_GROWTH_THRESHOLD = 0.10  # 10%, as a fraction to match yfinance's revenueGrowth

# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------


def format_row(rank, r):
    m = r["metrics"]
    growth = m.get("revenue_growth")
    growth_str = f"{growth * 100:.1f}%" if growth is not None else "n/a"
    price_str = f"${m['price']:.2f}" if m.get("price") else "n/a"
    daily_str = f"({m['daily_pct']:+.2f}%)" if m.get("daily_pct") is not None else ""

    return (
        f"#{rank:<5} {r['ticker']:<7} {r['sector']:<26} "
        f"growth: {growth_str:<8} score: {r['score']:<7} {price_str} {daily_str}"
    )


def build_report(total_tickers, passed):
    lines = []
    lines.append("=" * 84)
    lines.append("S&P 500 GROWTH SCREENER (revenue growth >= 10%)" + ("  [TEST MODE]" if screener.TEST_MODE else ""))
    lines.append("=" * 84)
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    pass_pct = (len(passed) / total_tickers * 100) if total_tickers else 0
    lines.append(f"\nScanned {total_tickers} S&P 500 tickers -- {len(passed)} passed the >=10% revenue growth filter ({pass_pct:.1f}%)")

    lines.append(f"\n--- RANKED ({len(passed)} passed) ---")
    for rank, r in enumerate(passed, start=1):
        lines.append(format_row(rank, r))

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    by_sector = screener.load_constituents()
    total_tickers = sum(len(t) for t in by_sector.values())
    print(f"Screening {total_tickers} tickers across {len(by_sector)} sectors...")
    if screener.TEST_MODE:
        print("(TEST_MODE is on -- small ticker slice per sector)")

    all_results = []
    for sector, tickers in by_sector.items():
        all_results.extend(screener.score_sector(sector, tickers))

    scored = [r for r in all_results if r["score"] is not None]
    passed = [
        r for r in scored
        if r["metrics"].get("revenue_growth") is not None
        and r["metrics"]["revenue_growth"] >= REVENUE_GROWTH_THRESHOLD
    ]
    passed.sort(key=lambda r: r["score"], reverse=True)

    report = build_report(total_tickers, passed)
    print("\n" + report)

    with open("growth_screener_results.txt", "w") as f:
        f.write(report)
    print("Saved to growth_screener_results.txt")


if __name__ == "__main__":
    if "--test" in sys.argv:
        screener.TEST_MODE = True
        screener.TEST_TICKERS_PER_SECTOR = 2
        print("[--test] running with screener.TEST_MODE on (small ticker slice per sector)\n")

    main()
