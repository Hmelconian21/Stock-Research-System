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
import re
import time
import xml.etree.ElementTree as ET

import requests
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
RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"  # news fallback, see get_news_for_ticker()

# Headline relevance matching (see is_relevant_headline). Suffixes are dropped
# from the end of a company name before matching ("Albemarle Corporation" ->
# "Albemarle"). A name's first word is also matched on its own ("Micron"),
# except for these: ordinary words, first names, places, and initials that
# would let unrelated headlines through.
COMPANY_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "companies",
                    "ltd", "limited", "plc", "llc", "lp", "holdings", "group", "the", "sa", "nv"}
GENERIC_FIRST_WORDS = {
    "Advanced", "Align", "Applied", "Arch", "Arthur", "Automatic", "Avery", "Baker", "Bank",
    "Best", "Boston", "Brown", "Builders", "C.H.", "Capital", "Cardinal", "Carrier", "Charter",
    "Church", "Citizens", "Comfort", "Consolidated", "Crown", "Digital", "Duke", "Edison",
    "Equity", "Erie", "Expand", "Extra", "Fair", "Federal", "Fifth", "First", "Franklin",
    "Genuine", "Global", "Globe", "Henry", "Home", "Host", "Illinois", "Interactive",
    "Intercontinental", "Invitation", "Iron", "J.B.", "J.M.", "Jack", "Kinder", "Live",
    "Marathon", "Martin", "Mid-America", "Monster", "Morgan", "Northern", "Packaging", "Palo",
    "Philip", "Phillips", "Pinnacle", "Principal", "Quest", "Ralph", "Raymond", "Realty",
    "Regency", "Regions", "Republic", "Ross", "Royal", "Simon", "Southwest", "State", "Steel",
    "Trade", "Tractor", "U.S.", "Union", "Universal", "Walt", "Warner", "Waste", "Wells", "West",
    "Western", "Willis",
}
TOP_N_FOR_NEWS = 5           # only pull news for the top N ranked stocks

# ---------------------------------------------------------------------------
# LOAD S&P 500 CONSTITUENTS
# ---------------------------------------------------------------------------


def load_constituents():
    """Returns {sector: [tickers]} from the real, current S&P 500 list, with
    one ticker per company.

    Some companies are in the index under two share classes (GOOGL/GOOG,
    FOXA/FOX, NWSA/NWS). Both classes have near-identical fundamentals, so
    keeping both would count the company twice as its own sector peer and
    let it take two slots in the rankings. Share classes share an SEC CIK, so
    only the first ticker listed for each CIK is kept."""
    by_sector = {}
    seen_ciks = {}
    with open(CONSTITUENTS_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row["Symbol"].replace(".", "-")  # yfinance uses BRK-B not BRK.B
            cik = row["CIK"]
            if cik in seen_ciks:
                print(f"  skipping {ticker}: same company as {seen_ciks[cik]} (another share class)")
                continue
            seen_ciks[cik] = ticker
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


_company_names = None      # {ticker: [name words]}, loaded on first use
_first_word_counts = None  # {first word: how many S&P companies start with it}


def company_name_variants(ticker):
    """Names a headline might use for this company, from the constituents
    file: the full name with legal suffixes and "(The)"/"(Class A)" removed
    ("Micron Technology"), plus its first word as a short name ("Micron")
    unless that word is shared by another S&P company or is a generic word
    (see GENERIC_FIRST_WORDS)."""
    global _company_names, _first_word_counts
    if _company_names is None:
        with open(CONSTITUENTS_FILE) as f:
            _company_names = {r["Symbol"].replace(".", "-"): _strip_company_name(r["Security"])
                              for r in csv.DictReader(f)}
        _first_word_counts = {}
        for words in _company_names.values():
            if words:
                _first_word_counts[words[0]] = _first_word_counts.get(words[0], 0) + 1

    words = _company_names.get(ticker)
    if not words:
        return []
    variants = [" ".join(words)]
    first = words[0]
    if (len(words) > 1 and len(first) >= 4 and first not in GENERIC_FIRST_WORDS
            and _first_word_counts[first] == 1):
        variants.append(first)
    return variants


def _strip_company_name(name):
    name = re.sub(r"\(.*?\)", " ", name)  # "(The)", "(Class A)"
    words = [w for w in re.split(r"[\s,]+", name) if w]
    while words and words[-1].lower().strip(".") in COMPANY_SUFFIXES:
        words.pop()
    while words and words[0].lower() == "the":
        words.pop(0)
    return words


def _mentions(text, phrase, ignore_case):
    pattern = rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE if ignore_case else 0) is not None


def is_relevant_headline(headline, ticker):
    """True if the headline mentions the ticker or the company's name.
    Tickers match case-sensitively (so "ALL" doesn't match "all"); one-letter
    tickers like F only count as "(F)" or "$F", since a bare capital letter is
    too common."""
    for symbol in {ticker, ticker.replace("-", ".")}:
        if len(symbol) == 1:
            if f"({symbol})" in headline or f"${symbol}" in headline:
                return True
        elif _mentions(headline, symbol, ignore_case=False):
            return True
    return any(_mentions(headline, name, ignore_case=True) for name in company_name_variants(ticker))


def get_rss_headlines(ticker, max_items=3, filter_relevant=True):
    """Headlines from Yahoo Finance's RSS feed for a ticker. The feed mixes in
    loosely related market stories, so by default only headlines that mention
    the ticker or company are kept -- fewer than max_items if that's all
    there is, rather than padding with unrelated ones."""
    resp = requests.get(RSS_URL, params={"s": ticker, "region": "US", "lang": "en-US"},
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    titles = [i.findtext("title") for i in ET.fromstring(resp.content).findall("./channel/item")]
    titles = [t for t in titles if t]
    if filter_relevant:
        titles = [t for t in titles if is_relevant_headline(t, ticker)]
    return titles[:max_items]


def get_news_for_ticker(ticker, max_items=3):
    """Recent news headlines tied to a specific ticker.

    Tries yfinance first, then falls back to Yahoo's RSS feed. As of
    yfinance 1.5-1.7, .news silently returns [] because the Yahoo endpoint
    it calls (/xhr/ncp) responds 404 -- the RSS feed still works."""
    try:
        news = yf.Ticker(ticker).news or []
        items = []
        for n in news[:max_items]:
            content = n.get("content", n)  # yfinance news schema varies by version
            title = content.get("title") or n.get("title")
            if title:
                items.append(title)
        if not items:
            # index tickers (^GSPC etc.) are for general market headlines, so don't filter those
            items = get_rss_headlines(ticker, max_items, filter_relevant=not ticker.startswith("^"))
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


def percentile_rank(value, peer_values, lower_is_better):
    """Percent of sector peers this stock beats on one metric, 0-100 (ties
    count as half). Replaces the old "% better than sector average" score,
    which divided by the average and exploded when it was near zero (e.g. a
    +6,500% FCF-yield score). A rank stays bounded no matter how small or
    skewed the sector's values are. peer_values includes this stock's own
    value, so one copy of it is dropped before comparing."""
    if value is None or not peer_values:
        return None
    others = list(peer_values)
    others.remove(value)
    if not others:
        return None
    if lower_is_better:
        beaten = sum(1 for v in others if v > value)
    else:
        beaten = sum(1 for v in others if v < value)
    ties = sum(1 for v in others if v == value)
    return (beaten + 0.5 * ties) / len(others) * 100


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
    peer_vals = {}
    for metric in WEIGHTS:
        vals = [clip_metric(metric, d[metric]) for d in all_data.values() if d.get(metric) is not None]
        avgs[metric] = sum(vals) / len(vals) if vals else None
        peer_vals[metric] = vals

    results = []
    for ticker, data in all_data.items():
        if data["eps"] is None or data["eps"] <= 0:
            results.append({"ticker": ticker, "status": "SKIPPED (no/negative earnings)", "score": None})
            continue

        pct_scores = {
            m: percentile_rank(clip_metric(m, data.get(m)), peer_vals.get(m), m in LOWER_IS_BETTER)
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

# Display only -- scoring and METRIC_CLIP keep yfinance's raw units. yfinance
# reports ROE and revenue growth as fractions (0.88 = 88%), but debtToEquity
# is already a percentage (40.5 = 40.5%), and fcf_yield is computed as a
# percentage in get_stock_data(). PEG and EV/EBITDA are plain ratios.
PERCENT_SCALE = {"roe": 100, "revenue_growth": 100, "fcf_yield": 1, "debt_to_equity": 1}


def metric_label(metric):
    """Report label for a metric, flagging the ones where a lower value scores better."""
    label = METRIC_LABELS[metric]
    return f"{label} (lower is better)" if metric in LOWER_IS_BETTER else label


def format_metric(metric, value):
    if value is None:
        return "n/a"
    if metric in PERCENT_SCALE:
        return f"{value * PERCENT_SCALE[metric]:.1f}%"
    return f"{value:.2f}"


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
        for metric in METRIC_LABELS:
            label = metric_label(metric)
            val = m.get(metric)
            avg = avgs.get(metric)
            p = pct.get(metric)
            if val is None:
                lines.append(f"    {label}: n/a")
            else:
                p_str = f"(beats {p:.0f}% of sector peers)" if p is not None else ""
                clip_str = " [clipped for scoring]" if clip_metric(metric, val) != val else ""
                lines.append(f"    {label}: {format_metric(metric, val)}{clip_str}  |  "
                             f"sector avg: {format_metric(metric, avg)}  {p_str}")

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
