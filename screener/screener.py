"""
Relative Valuation Screener (v4 - Full Index)
------------------------------------------------
Screens the S&P 500 (real constituent list with actual GICS sectors, not a
hand-picked peer list), scores each stock against its true sector average
across six fundamentals metrics, shows live price + daily % change, and
pulls news for both the overall market and your top-ranked results.

FIRST RUN: Use TEST_MODE = True (default) to run on a small slice first.
Full S&P 500 takes a while -- see the note above main() for realistic timing.

Run: python3 screener.py
Outputs: prints to terminal AND writes screener_results.txt

Requires:
  pip install yfinance --break-system-packages
  sp500_constituents.csv in the shared ~/market-data folder (ticker,sector
  data -- see CONSTITUENTS_FILE below; also used by statement_analyzer.py
  and combined_report.py so there's one copy, not one per project folder)
"""

import csv
import os
import time
import yfinance as yf

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# True = only screen the first N tickers per sector (fast, good for testing).
# False = screen the full S&P 500 (slow -- see timing note near main()).
TEST_MODE = False
TEST_TICKERS_PER_SECTOR = 3

CONSTITUENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "market-data", "sp500_constituents.csv")

WEIGHTS = {
    "peg": 0.25,
    "ev_ebitda": 0.25,
    "roe": 0.15,
    "revenue_growth": 0.15,
    "fcf_yield": 0.10,
    "debt_to_equity": 0.10,
}
LOWER_IS_BETTER = {"peg", "ev_ebitda", "debt_to_equity"}

# Sane bounds per metric, used only for sector-average and score math -- raw
# values still print in the report untouched. yfinance occasionally returns
# wild figures (e.g. ROE in the thousands of percent, D/E over 50,000%) from
# things like negative equity or one-off accounting events; without clipping,
# a single bad data point can dominate a whole sector average and blow out
# the composite score for every peer in that sector, not just the outlier.
METRIC_CLIP = {
    "peg": (0, 10),
    "ev_ebitda": (0, 100),
    "roe": (-2, 3),
    "revenue_growth": (-1, 3),
    "fcf_yield": (-50, 50),
    "debt_to_equity": (0, 500),
}

REQUEST_DELAY_SECONDS = 0.3  # be polite to Yahoo's free endpoint, avoid rate limits
TOP_N_FOR_NEWS = 5           # only pull news for the top N ranked stocks

# ---------------------------------------------------------------------------
# LOAD S&P 500 CONSTITUENTS
# ---------------------------------------------------------------------------


def load_constituents():
    """Returns {sector: [tickers]} from the real, current S&P 500 list."""
    by_sector = {}
    with open(CONSTITUENTS_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row["Symbol"].replace(".", "-")  # yfinance uses BRK-B not BRK.B
            sector = row["GICS Sector"]
            by_sector.setdefault(sector, []).append(ticker)

    if TEST_MODE:
        by_sector = {s: t[:TEST_TICKERS_PER_SECTOR] for s, t in by_sector.items()}

    return by_sector


# ---------------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------------


def get_stock_data(ticker):
    """Fetch fundamentals + live price data for a ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.info

        market_cap = info.get("marketCap")
        free_cashflow = info.get("freeCashflow")
        fcf_yield = None
        if market_cap and free_cashflow and market_cap > 0:
            fcf_yield = free_cashflow / market_cap * 100

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        prev_close = info.get("previousClose")
        daily_pct = None
        if price is not None and prev_close:
            daily_pct = (price - prev_close) / prev_close * 100

        return {
            "ticker": ticker,
            "quote_type": info.get("quoteType"),
            "sector": info.get("sector"),
            "eps": info.get("trailingEps"),
            "peg": info.get("trailingPegRatio") or info.get("pegRatio"),
            "ev_ebitda": info.get("enterpriseToEbitda"),
            "roe": info.get("returnOnEquity"),
            "revenue_growth": info.get("revenueGrowth"),
            "debt_to_equity": info.get("debtToEquity"),
            "fcf_yield": fcf_yield,
            "price": price,
            "daily_pct": daily_pct,
        }
    except Exception as e:
        print(f"  [warning] couldn't fetch {ticker}: {e}")
        return None
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)


def get_news_for_ticker(ticker, max_items=3):
    """Recent news headlines tied to a specific ticker."""
    try:
        news = yf.Ticker(ticker).news or []
        items = []
        for n in news[:max_items]:
            content = n.get("content", n)  # yfinance news schema varies by version
            title = content.get("title") or n.get("title")
            if title:
                items.append(title)
        return items
    except Exception as e:
        print(f"  [warning] couldn't fetch news for {ticker}: {e}")
        return []
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)


def get_market_headlines(max_items=5):
    """General market-moving headlines, sourced from major index tickers."""
    headlines = []
    for index_ticker in ["^GSPC", "^DJI", "^IXIC"]:
        headlines.extend(get_news_for_ticker(index_ticker, max_items=2))
    # de-dupe while preserving order
    seen = set()
    unique = []
    for h in headlines:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return unique[:max_items]


# ---------------------------------------------------------------------------
# SCORING (same logic as v3, now driven by real sector membership)
# ---------------------------------------------------------------------------


def clip_metric(metric, value):
    """Bound a raw metric value into its sane range before it's used in any
    averaging or scoring math. Returns the value unchanged if there's no
    clip range defined for that metric, or if value is None."""
    if value is None:
        return None
    bounds = METRIC_CLIP.get(metric)
    if bounds is None:
        return value
    lo, hi = bounds
    return max(lo, min(hi, value))


def pct_better(value, average, lower_is_better):
    if value is None or average is None or average == 0:
        return None
    if lower_is_better:
        return (average - value) / abs(average) * 100
    return (value - average) / abs(average) * 100


def composite_score(pct_scores):
    available = {m: s for m, s in pct_scores.items() if s is not None}
    if not available:
        return None
    total_weight = sum(WEIGHTS[m] for m in available)
    if total_weight == 0:
        return None
    return sum(WEIGHTS[m] * s for m, s in available.items()) / total_weight


def score_sector(sector, tickers):
    """Fetch every ticker in a sector once, compute peer averages, then score each."""
    print(f"\nSector: {sector} ({len(tickers)} tickers)")

    all_data = {}
    for ticker in tickers:
        print(f"  fetching {ticker}...")
        data = get_stock_data(ticker)
        if data and data.get("quote_type") == "EQUITY":
            all_data[ticker] = data

    # sector averages from whatever data we successfully got, clipped so one
    # wild data point can't drag the whole sector's average off course
    avgs = {}
    for metric in WEIGHTS:
        vals = [clip_metric(metric, d[metric]) for d in all_data.values() if d.get(metric) is not None]
        avgs[metric] = sum(vals) / len(vals) if vals else None

    results = []
    for ticker, data in all_data.items():
        if data["eps"] is None or data["eps"] <= 0:
            results.append({"ticker": ticker, "status": "SKIPPED (no/negative earnings)", "score": None})
            continue

        pct_scores = {
            m: pct_better(clip_metric(m, data.get(m)), avgs.get(m), m in LOWER_IS_BETTER)
            for m in WEIGHTS
        }
        score = composite_score(pct_scores)

        results.append({
            "ticker": ticker,
            "status": "SCORED",
            "score": round(score, 1) if score is not None else None,
            "sector": sector,
            "metrics": data,
            "peer_avgs": avgs,
            "pct_scores": pct_scores,
        })

    return results


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

METRIC_LABELS = {
    "peg": "PEG",
    "ev_ebitda": "EV/EBITDA",
    "roe": "ROE",
    "revenue_growth": "Revenue growth",
    "fcf_yield": "FCF yield",
    "debt_to_equity": "Debt/Equity",
}


def build_report(all_results, market_headlines):
    lines = []
    lines.append("=" * 84)
    lines.append("S&P 500 RELATIVE VALUATION SCREENER" + ("  [TEST MODE]" if TEST_MODE else ""))
    lines.append("=" * 84)

    lines.append("\n--- MARKET HEADLINES ---")
    if market_headlines:
        for h in market_headlines:
            lines.append(f"  - {h}")
    else:
        lines.append("  (none fetched)")

    scored = [r for r in all_results if r["score"] is not None]
    unscored = [r for r in all_results if r["score"] is None]
    scored.sort(key=lambda r: r["score"], reverse=True)

    lines.append(f"\n--- RANKED ({len(scored)} scored) ---")
    for rank, r in enumerate(scored, start=1):
        m = r["metrics"]
        price_str = f"${m['price']:.2f}" if m.get("price") else "n/a"
        daily_str = f"({m['daily_pct']:+.2f}% today)" if m.get("daily_pct") is not None else ""
        lines.append(f"\n#{rank}  {r['ticker']}  |  score: {r['score']}  |  {r['sector']}  |  {price_str} {daily_str}")

        avgs, pct = r["peer_avgs"], r["pct_scores"]
        for metric, label in METRIC_LABELS.items():
            val = m.get(metric)
            avg = avgs.get(metric)
            p = pct.get(metric)
            if val is None:
                lines.append(f"    {label}: n/a")
            else:
                p_str = f"({p:+.1f}% vs sector)" if p is not None else ""
                avg_str = f"{avg:.2f}" if avg is not None else "n/a"
                clip_str = " [clipped for scoring]" if clip_metric(metric, val) != val else ""
                lines.append(f"    {label}: {val:.2f}{clip_str}  |  sector avg: {avg_str}  {p_str}")

        if rank <= TOP_N_FOR_NEWS:
            headlines = get_news_for_ticker(r["ticker"])
            if headlines:
                lines.append("    recent news:")
                for h in headlines:
                    lines.append(f"      - {h}")

    if unscored:
        lines.append(f"\n--- NOT SCORED ({len(unscored)}) ---")
        for r in unscored:
            lines.append(f"{r['ticker']}: {r['status']}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
#
# TIMING NOTE: each ticker fetch takes roughly 0.5-1 second including the
# polite delay. TEST_MODE (3 tickers x 11 sectors = ~33 tickers) takes about
# a minute. The full S&P 500 (~500 tickers) could take 10-20+ minutes and
# has a real chance of hitting Yahoo's rate limits on a free connection.
# Recommend running TEST_MODE first, then trying a full run when you don't
# need the terminal for anything else.
# ---------------------------------------------------------------------------


def main():
    by_sector = load_constituents()
    total_tickers = sum(len(t) for t in by_sector.values())
    print(f"Screening {total_tickers} tickers across {len(by_sector)} sectors...")
    if TEST_MODE:
        print("(TEST_MODE is on -- set TEST_MODE = False at the top of the file for the full S&P 500)")

    all_results = []
    for sector, tickers in by_sector.items():
        all_results.extend(score_sector(sector, tickers))

    print("\nFetching market headlines...")
    market_headlines = get_market_headlines()

    report = build_report(all_results, market_headlines)
    print("\n" + report)

    with open("screener_results.txt", "w") as f:
        f.write(report)
    print("Saved to screener_results.txt")


if __name__ == "__main__":
    main()
