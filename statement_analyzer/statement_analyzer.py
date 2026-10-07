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
from datetime import date, timedelta
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

# SEC filings report each income-statement/cash-flow figure for several
# period lengths at once (quarter, year-to-date, full year), and businesses
# are seasonal, so comparing a company's two most recent periods can pair a
# holiday quarter against a slow one. Every check instead compares like with
# like: the latest quarter against the SAME quarter a year earlier, or
# trailing-twelve-month (TTM) totals. Period lengths and "a year earlier" are
# matched within this many days, which also absorbs 52/53-week fiscal years.
PERIOD_LENGTH_TOLERANCE_DAYS = 20
QUARTER_DAYS = (80, 100)   # duration range treated as a single quarter
YEAR_DAYS = 365
STALE_AFTER_DAYS = 400     # see is_stale()

# Possible SEC XBRL tag names for each concept (companies don't all use the same tag)
REVENUE_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]
RECEIVABLES_TAGS = ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent", "AccountsAndOtherReceivablesNetCurrent"]
# ProfitLoss includes minority interests, like consolidated operating cash flow does
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]
OCF_TAGS = ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
GROSS_PROFIT_TAGS = ["GrossProfit"]
COGS_TAGS = ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold",
             "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"]

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


def _days(start, end):
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _shift(day, days):
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _filed_entries(facts, tag_list):
    """USD values from actual 10-K/10-Q filings, from whichever of the tags
    has the most recent data -- companies switch tags over time (e.g. from
    Revenues to RevenueFromContractWithCustomer...), so the first tag found
    can hold only stale years."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    best = []
    for tag in tag_list:
        entries = [e for e in gaap.get(tag, {}).get("units", {}).get("USD", []) if e.get("form") in ("10-K", "10-Q")]
        if entries and (not best or max(e["end"] for e in entries) > max(e["end"] for e in best)):
            best = entries
    return best


def get_periods(facts, tag_list):
    """Duration facts (revenue, net income, cash flow...) as {(start, end): value}.
    A later filing's value for the same period overwrites an earlier one."""
    return {(e["start"], e["end"]): e["val"] for e in _filed_entries(facts, tag_list) if e.get("start")}


def get_revenue_periods(facts):
    """Revenue by period, filling any gaps with gross profit + cost of goods
    sold for the same period. Some companies (e.g. Best Buy since mid-2025)
    stop tagging quarterly revenue while still tagging both of those."""
    revenue = get_periods(facts, REVENUE_TAGS)
    gross_profit = get_periods(facts, GROSS_PROFIT_TAGS)
    cogs = get_periods(facts, COGS_TAGS)
    for period in gross_profit.keys() & cogs.keys() - revenue.keys():
        revenue[period] = gross_profit[period] + cogs[period]
    return revenue


def get_instants(facts, tag_list):
    """Point-in-time facts (balance-sheet items like receivables) as {end: value}."""
    return {e["end"]: e["val"] for e in _filed_entries(facts, tag_list) if not e.get("start")}


def _find_period(periods, end, length):
    """The period ending within tolerance of `end` whose length is within
    tolerance of `length` days, as (start, end, value), or None."""
    tol = PERIOD_LENGTH_TOLERANCE_DAYS
    matches = [(s, e, v) for (s, e), v in periods.items()
               if abs(_days(e, end)) <= tol and abs(_days(s, e) - length) <= tol]
    return min(matches, key=lambda m: abs(_days(m[1], end)), default=None)


def is_stale(facts, day):
    """True if `day` is more than STALE_AFTER_DAYS before the company's newest
    10-K/10-Q data -- i.e. the tag being compared stopped being reported
    (companies switch XBRL tags), so a comparison would use old years.
    Forward-looking schedules (e.g. next year's amortization) carry end
    dates after the filing that reports them, so those are ignored."""
    newest = max((e["end"] for d in facts.get("facts", {}).get("us-gaap", {}).values()
                  for e in d.get("units", {}).get("USD", [])
                  if e.get("form") in ("10-K", "10-Q") and e["end"] <= e.get("filed", e["end"])), default=day)
    return _days(day, newest) > STALE_AFTER_DAYS


def _value_near(instants, day):
    """Balance-sheet value dated within tolerance of `day`, or None."""
    near = [(abs(_days(d, day)), v) for d, v in instants.items() if abs(_days(d, day)) <= PERIOD_LENGTH_TOLERANCE_DAYS]
    return min(near)[1] if near else None


def latest_quarter_yoy(periods):
    """(this_quarter, same_quarter_last_year), each as (start, end, value),
    for the most recent single quarter that also has a year-ago match. Falls
    back to the latest full year vs the year before if there's no quarterly
    pair (a fiscal Q4 is only reported inside the annual total)."""
    for low, high in (QUARTER_DAYS, (YEAR_DAYS - PERIOD_LENGTH_TOLERANCE_DAYS, YEAR_DAYS + PERIOD_LENGTH_TOLERANCE_DAYS)):
        candidates = sorted(((s, e, v) for (s, e), v in periods.items() if low <= _days(s, e) <= high),
                            key=lambda m: m[1], reverse=True)
        for current in candidates[:2]:  # newest, or the one before if the newest has no year-ago match
            prior = _find_period(periods, _shift(current[1], -YEAR_DAYS), _days(current[0], current[1]))
            if prior:
                return current, prior
    return None


def ttm(periods, end):
    """Trailing-twelve-month total for the period ending at `end`, as
    (value, description) or None. Uses the full year if one ends there;
    otherwise last full year + this year-to-date - last year's same
    year-to-date. Cash flow is only reported year-to-date, so this is the
    only way to get a seasonally fair twelve-month figure mid-year."""
    annual = _find_period(periods, end, YEAR_DAYS)
    if annual:
        return annual[2], f"fiscal year ended {annual[1]}"
    ytd = max(((s, e, v) for (s, e), v in periods.items()
               if abs(_days(e, end)) <= PERIOD_LENGTH_TOLERANCE_DAYS
               and _days(s, e) < YEAR_DAYS - PERIOD_LENGTH_TOLERANCE_DAYS),
              key=lambda m: _days(m[0], m[1]), default=None)
    if not ytd:
        return None
    prior_ytd = _find_period(periods, _shift(ytd[1], -YEAR_DAYS), _days(ytd[0], ytd[1]))
    last_year = _find_period(periods, _shift(ytd[0], -1), YEAR_DAYS)
    if not (prior_ytd and last_year):
        return None
    return last_year[2] + ytd[2] - prior_ytd[2], f"12 months ended {ytd[1]}"


# ---------------------------------------------------------------------------
# RED FLAG CHECKS
# ---------------------------------------------------------------------------


def pct_change(newer, older):
    if older == 0:
        return None
    return (newer - older) / abs(older) * 100


def check_receivables_vs_revenue(facts):
    """Year-over-year growth in quarterly revenue vs. receivables on the same two dates."""
    pair = latest_quarter_yoy(get_revenue_periods(facts))
    receivables = get_instants(facts, RECEIVABLES_TAGS)
    if not pair or not receivables:
        return {"flag": None, "note": "insufficient data"}

    (_, end_now, rev_now), (_, end_then, rev_then) = pair
    if is_stale(facts, end_now):
        return {"flag": None, "note": f"insufficient data (latest tagged quarterly revenue is from {end_now})"}
    recv_now, recv_then = _value_near(receivables, end_now), _value_near(receivables, end_then)
    if recv_now is None or recv_then is None:
        return {"flag": None, "note": "insufficient data"}

    rev_growth = pct_change(rev_now, rev_then)
    recv_growth = pct_change(recv_now, recv_then)
    if rev_growth is None or recv_growth is None:
        return {"flag": None, "note": "insufficient data"}

    gap = recv_growth - rev_growth
    return {
        "flag": gap >= RECEIVABLES_VS_REVENUE_GAP_PP,
        "note": f"year over year to {end_now}: revenue grew {rev_growth:.1f}%, "
                f"receivables grew {recv_growth:.1f}% (gap: {gap:.1f}pp)",
    }


def check_income_vs_cashflow(facts):
    """Trailing-twelve-month operating cash flow vs. net income for the same twelve months."""
    ocf_periods = get_periods(facts, OCF_TAGS)
    ni_periods = get_periods(facts, NET_INCOME_TAGS)
    if not ocf_periods or not ni_periods:
        return {"flag": None, "note": "insufficient data"}

    # newest date with a TTM figure for both (cash flow is the limiting one)
    for end in sorted({e for _, e in ocf_periods}, reverse=True)[:4]:
        ocf, ni = ttm(ocf_periods, end), ttm(ni_periods, end)
        if ocf and ni:
            break
    else:
        return {"flag": None, "note": "insufficient data"}

    (ocf_val, label), (ni_val, _) = ocf, ni
    if is_stale(facts, end):
        return {"flag": None, "note": f"insufficient data (latest tagged cash flow is from {end})"}
    if ni_val <= 0:
        return {"flag": None, "note": "net income negative/zero, ratio not meaningful"}

    ratio = ocf_val / ni_val
    return {
        "flag": ratio < OCF_TO_NET_INCOME_MIN_RATIO,
        "note": f"{label}: operating cash flow is {ratio:.2f}x net income "
                f"(net income: {ni_val:,.0f}, OCF: {ocf_val:,.0f})",
    }


def check_gross_margin_jump(facts):
    """Latest quarter's gross margin vs. the same quarter a year earlier."""
    pair = latest_quarter_yoy(get_revenue_periods(facts))
    if not pair:
        return {"flag": None, "note": "insufficient data"}

    if is_stale(facts, pair[0][1]):
        return {"flag": None, "note": f"insufficient data (latest tagged quarterly revenue is from {pair[0][1]})"}
    gross_profit = get_periods(facts, GROSS_PROFIT_TAGS)
    cogs = get_periods(facts, COGS_TAGS)
    margins = []
    for start, end, rev in pair:
        if not rev:
            return {"flag": None, "note": "insufficient data"}
        if (start, end) in gross_profit:
            gp = gross_profit[(start, end)]
        elif (start, end) in cogs:  # derive gross profit when GrossProfit isn't reported
            gp = rev - cogs[(start, end)]
        else:
            return {"flag": None, "note": "insufficient data"}
        margins.append(gp / rev * 100)

    now, then = margins
    jump = now - then
    return {
        "flag": abs(jump) >= GROSS_MARGIN_JUMP_PP,
        "note": f"quarter ended {pair[0][1]} vs a year earlier: gross margin "
                f"{then:.1f}% -> {now:.1f}% ({jump:+.1f}pp change)",
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
