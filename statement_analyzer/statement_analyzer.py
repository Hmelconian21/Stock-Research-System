"""
Financial Statement Analyzer
--------------------------------
Pulls real financial statement data straight from the SEC (via EDGAR's
"company facts" API -- structured JSON, not PDF scraping) and checks for
three well-known earnings-quality red flags:

  1. Receivables growing much faster than revenue
     -> can mean the company is booking sales it hasn't collected cash for
  2. Net income growing while operating cash flow doesn't follow
     -> reported profit not backed by real cash (classic red flag)
  3. A sudden, large jump in gross margin
     -> worth a second look rather than an automatic celebration

Uses sp500_constituents.csv, shared from ~/market-data (same file screener.py
and combined_report.py read), to map ticker -> CIK, since SEC's API needs
the CIK, not the ticker symbol.

IMPORTANT: The SEC requires a real User-Agent identifying who's making
requests (their fair-access policy). Set the SEC_USER_AGENT_EMAIL env var before running,
or SEC's servers will reject the requests.

Run: python3 statement_analyzer.py TICKER
Example: python3 statement_analyzer.py NVDA

Requires: pip install requests --break-system-packages
"""

import sys
import csv
import os
from datetime import date
import requests

# ---------------------------------------------------------------------------
# CONFIG -- edit this before running
# ---------------------------------------------------------------------------

YOUR_EMAIL = os.environ.get("SEC_USER_AGENT_EMAIL", "")  # SEC requires a contact email in the User-Agent
HEADERS = {"User-Agent": f"stock-research-system personal research tool {YOUR_EMAIL}"}

CONSTITUENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "market-data", "sp500_constituents.csv")

# Thresholds -- adjust if these end up too strict/loose once you see real output
RECEIVABLES_VS_REVENUE_GAP_PP = 15   # flag if receivables growth outpaces revenue growth by this many percentage points
OCF_TO_NET_INCOME_MIN_RATIO = 0.7    # flag if operating cash flow is below this fraction of net income
GROSS_MARGIN_JUMP_PP = 5             # flag if gross margin jumps by more than this many percentage points period-over-period

# Duration facts (revenue, net income, etc.) get compared period-over-period.
# SEC filings mix quarterly (10-Q) and annual (10-K) periods for the same tag,
# so the two most recent entries by end-date are often a quarter and a full
# year -- not comparable. This tolerance (in days) is used to only compare
# entries that cover roughly the same length of time as the most recent one.
PERIOD_LENGTH_TOLERANCE_DAYS = 20

# Possible SEC XBRL tag names for each concept (companies don't all use the same tag)
REVENUE_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]
RECEIVABLES_TAGS = ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]
NET_INCOME_TAGS = ["NetIncomeLoss"]
OCF_TAGS = ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
GROSS_PROFIT_TAGS = ["GrossProfit"]
COGS_TAGS = ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"]

# ---------------------------------------------------------------------------
# TICKER -> CIK LOOKUP
# ---------------------------------------------------------------------------


def get_cik(ticker):
    with open(CONSTITUENTS_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Symbol"].replace(".", "-") == ticker.upper():
                return row["CIK"].zfill(10)
    return None


# ---------------------------------------------------------------------------
# SEC DATA FETCHING
# ---------------------------------------------------------------------------


def fetch_company_facts(cik):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_series(facts, tag_list, instant=False):
    """Pull a USD time series for the first matching tag found. Returns a
    list of (end_date, value) tuples sorted most-recent-first, restricted to
    actual filed 10-K/10-Q values.

    Balance-sheet items (instant=True, e.g. receivables) are point-in-time
    snapshots, so any two dates are comparable as-is.

    Income-statement/cash-flow items (instant=False, e.g. revenue, net
    income) cover a *duration* (start -> end). SEC filings report both
    quarterly (10-Q, ~90 days) and annual (10-K, ~365 days) durations for
    the same tag, so naively taking the two most recent end-dates can pair
    a quarter against a full year. To avoid that, entries are filtered down
    to only those whose duration is close to the most recent entry's
    duration before returning.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tag_list:
        if tag not in gaap:
            continue
        entries = gaap[tag].get("units", {}).get("USD", [])
        filed = [e for e in entries if e.get("form") in ("10-K", "10-Q")]

        if instant:
            by_date = {}
            for e in filed:
                by_date[e["end"]] = e["val"]  # later entries overwrite dupes, fine for v1
            sorted_series = sorted(by_date.items(), key=lambda x: x[0], reverse=True)
            if sorted_series:
                return sorted_series
            continue

        by_period = {}
        for e in filed:
            start, end = e.get("start"), e.get("end")
            if not start or not end:
                continue
            by_period[(start, end)] = e["val"]  # later entries overwrite dupes, fine for v1
        if not by_period:
            continue

        def duration_days(start, end):
            return (date.fromisoformat(end) - date.fromisoformat(start)).days

        periods = sorted(by_period.items(), key=lambda item: item[0][1], reverse=True)
        most_recent_days = duration_days(*periods[0][0])
        matched = [
            (end, val)
            for (start, end), val in periods
            if abs(duration_days(start, end) - most_recent_days) <= PERIOD_LENGTH_TOLERANCE_DAYS
        ]
        if matched:
            return matched
    return []


# ---------------------------------------------------------------------------
# RED FLAG CHECKS
# ---------------------------------------------------------------------------


def pct_change(newer, older):
    if older == 0:
        return None
    return (newer - older) / abs(older) * 100


def check_receivables_vs_revenue(facts):
    revenue = get_series(facts, REVENUE_TAGS)
    receivables = get_series(facts, RECEIVABLES_TAGS, instant=True)

    if len(revenue) < 2 or len(receivables) < 2:
        return {"flag": None, "note": "insufficient data"}

    rev_growth = pct_change(revenue[0][1], revenue[1][1])
    recv_growth = pct_change(receivables[0][1], receivables[1][1])

    if rev_growth is None or recv_growth is None:
        return {"flag": None, "note": "insufficient data"}

    gap = recv_growth - rev_growth
    flagged = gap >= RECEIVABLES_VS_REVENUE_GAP_PP

    return {
        "flag": flagged,
        "note": f"revenue grew {rev_growth:.1f}%, receivables grew {recv_growth:.1f}% (gap: {gap:.1f}pp)",
    }


def check_income_vs_cashflow(facts):
    net_income = get_series(facts, NET_INCOME_TAGS)
    ocf = get_series(facts, OCF_TAGS)

    if not net_income or not ocf:
        return {"flag": None, "note": "insufficient data"}

    latest_ni = net_income[0][1]
    latest_ocf = ocf[0][1]

    if latest_ni <= 0:
        return {"flag": None, "note": "net income negative/zero, ratio not meaningful"}

    ratio = latest_ocf / latest_ni
    flagged = ratio < OCF_TO_NET_INCOME_MIN_RATIO

    return {
        "flag": flagged,
        "note": f"operating cash flow is {ratio:.2f}x net income (net income: {latest_ni:,.0f}, OCF: {latest_ocf:,.0f})",
    }


def check_gross_margin_jump(facts):
    revenue = get_series(facts, REVENUE_TAGS)
    gross_profit = get_series(facts, GROSS_PROFIT_TAGS)

    if not gross_profit:
        # derive gross profit from revenue - COGS if GrossProfit tag isn't reported
        cogs = get_series(facts, COGS_TAGS)
        if not revenue or not cogs or len(revenue) < 2 or len(cogs) < 2:
            return {"flag": None, "note": "insufficient data"}
        margins = []
        for (rdate, rval), (cdate, cval) in zip(revenue[:2], cogs[:2]):
            if rval:
                margins.append((rdate, (rval - cval) / rval * 100))
    else:
        rev_by_date = dict(revenue)
        margins = []
        for date, gp in gross_profit[:2]:
            rev = rev_by_date.get(date)
            if rev:
                margins.append((date, gp / rev * 100))

    if len(margins) < 2:
        return {"flag": None, "note": "insufficient data"}

    jump = margins[0][1] - margins[1][1]
    flagged = abs(jump) >= GROSS_MARGIN_JUMP_PP

    return {
        "flag": flagged,
        "note": f"gross margin {margins[1][1]:.1f}% -> {margins[0][1]:.1f}% ({jump:+.1f}pp change)",
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def analyze(ticker):
    cik = get_cik(ticker)
    if cik is None:
        print(f"'{ticker}' not found in {CONSTITUENTS_FILE} (only S&P 500 tickers are covered)")
        return

    print(f"Fetching SEC filing data for {ticker} (CIK {cik})...")
    try:
        facts = fetch_company_facts(cik)
    except Exception as e:
        print(f"Error fetching from SEC: {e}")
        return

    checks = {
        "Receivables vs. revenue growth": check_receivables_vs_revenue(facts),
        "Net income vs. operating cash flow": check_income_vs_cashflow(facts),
        "Gross margin jump": check_gross_margin_jump(facts),
    }

    lines = []
    lines.append("=" * 70)
    lines.append(f"FINANCIAL STATEMENT ANALYSIS: {ticker}")
    lines.append("=" * 70)

    any_flags = False
    for name, result in checks.items():
        if result["flag"] is None:
            status = "N/A"
        elif result["flag"]:
            status = "FLAGGED"
            any_flags = True
        else:
            status = "OK"
        lines.append(f"\n{name}: {status}")
        lines.append(f"  {result['note']}")

    lines.append("\n" + ("Result: one or more red flags found -- worth digging deeper before trusting the numbers at face value." if any_flags else "Result: no red flags from these three checks."))
    lines.append("")

    report = "\n".join(lines)
    print("\n" + report)

    filename = f"statement_analysis_{ticker}.txt"
    with open(filename, "w") as f:
        f.write(report)
    print(f"Saved to {filename}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 statement_analyzer.py TICKER")
        print("Example: python3 statement_analyzer.py NVDA")
        sys.exit(1)

    analyze(sys.argv[1].upper())
