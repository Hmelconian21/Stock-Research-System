"""
Combined Valuation + Statement + News Report
---------------------------------------------
Runs the full S&P 500 valuation scan (reusing screener.py's own logic,
scoring, and clipping fix unchanged), takes the top 10 ranked stocks, and
for those 10 only, layers on:

  - statement_analyzer.py's three earnings-quality red-flag checks
    (with the period-length-matching fix)
  - a per-ticker news fetch (reusing screener.py's get_news_for_ticker,
    which already has the polite rate-limit delay built in)

General market headlines are fetched once and printed at the top of the
report, not repeated per stock.

This script does not reimplement any scoring, clipping, SEC-tag, or news
logic -- it imports the real functions from screener.py and
statement_analyzer.py so a fix made in either source file is picked up
here automatically.

Run: python3 combined_report.py            (full S&P 500 scan)
     python3 combined_report.py --test     (screener's TEST_MODE, small
                                             ticker slice per sector, for
                                             a quick end-to-end smoke run)

Outputs: prints to terminal AND writes combined_report.txt

Requires:
  pip install yfinance requests --break-system-packages
  sp500_constituents.csv in the shared ~/market-data folder (ticker,sector,
  CIK data -- both screener.py and statement_analyzer.py read it from
  there directly, so it no longer needs to sit next to any of the scripts)
"""

import sys
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

# screener.py and statement_analyzer.py live in their own project folders,
# not next to this script -- add both to sys.path so they can be imported
# as plain modules and their functions reused as-is.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screener"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "statement_analyzer"))

import screener
import statement_analyzer as stmt

TOP_N = 10  # how many top-ranked stocks get the statement + news deep dive

# ---------------------------------------------------------------------------
# EMAIL NOTIFICATION
# ---------------------------------------------------------------------------
# Read from environment variables -- never hardcode credentials.
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")                  # your Gmail address
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")        # 16-char Gmail App Password
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", GMAIL_ADDRESS)     # where to send it (defaults to GMAIL_ADDRESS)


def send_email_notification(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = NOTIFY_EMAIL
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def build_email_body(top_results):
    lines = [f"Top {len(top_results)} ranked:"]
    for rank, r in enumerate(top_results, start=1):
        lines.append(f"  #{rank}  {r['ticker']}  |  score: {r['score']}")

    flagged = []
    for r in top_results:
        checks = r.get("statement_checks")
        if not checks:
            continue
        fired = [name for name, result in checks.items() if result["flag"]]
        if fired:
            flagged.append((r["ticker"], fired))

    lines.append("")
    lines.append("Statement red flags:")
    if flagged:
        for ticker, fired in flagged:
            lines.append(f"  {ticker}: {', '.join(fired)}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# PER-STOCK DEEP DIVE (statement checks + news)
# ---------------------------------------------------------------------------


def run_statement_checks(ticker):
    """Reuses statement_analyzer.py's CIK lookup + red-flag checks as-is."""
    cik = stmt.get_cik(ticker)
    if cik is None:
        return None
    try:
        facts = stmt.fetch_company_facts(cik)
    except Exception as e:
        print(f"    [warning] statement analysis failed for {ticker}: {e}")
        return None

    return {
        "Receivables vs. revenue growth": stmt.check_receivables_vs_revenue(facts),
        "Net income vs. operating cash flow": stmt.check_income_vs_cashflow(facts),
        "Gross margin jump": stmt.check_gross_margin_jump(facts),
    }


def enrich_top_results(top_results):
    """For the top-ranked stocks only: attach statement checks + news."""
    print(f"\nRunning statement analysis + news for the top {len(top_results)}...")
    for r in top_results:
        ticker = r["ticker"]
        print(f"  {ticker}: statement checks...")
        r["statement_checks"] = run_statement_checks(ticker)

        print(f"  {ticker}: news...")
        r["news"] = screener.get_news_for_ticker(ticker)  # has its own rate-limit delay


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------


def format_valuation_lines(r):
    lines = []
    m = r["metrics"]
    avgs, pct = r["peer_avgs"], r["pct_scores"]
    for metric, label in screener.METRIC_LABELS.items():
        val = m.get(metric)
        avg = avgs.get(metric)
        p = pct.get(metric)
        if val is None:
            lines.append(f"      {label}: n/a")
        else:
            p_str = f"(beats {p:.0f}% of sector peers)" if p is not None else ""
            clip_str = " [clipped for scoring]" if screener.clip_metric(metric, val) != val else ""
            lines.append(f"      {label}: {screener.format_metric(metric, val)}{clip_str}  |  "
                         f"sector avg: {screener.format_metric(metric, avg)}  {p_str}")
    return lines


def format_statement_lines(checks):
    lines = []
    if checks is None:
        lines.append("      (no SEC CIK match / fetch failed -- skipped)")
        return lines
    for name, result in checks.items():
        if result["flag"] is None:
            status = "N/A"
        elif result["flag"]:
            status = "FLAGGED"
        else:
            status = "OK"
        lines.append(f"      {name}: {status}")
        lines.append(f"        {result['note']}")
    return lines


def format_news_lines(headlines):
    lines = []
    if headlines:
        for h in headlines:
            lines.append(f"      - {h}")
    else:
        lines.append("      (none fetched)")
    return lines


def build_report(top_results, market_headlines):
    lines = []
    lines.append("=" * 84)
    lines.append("S&P 500 COMBINED REPORT: VALUATION + STATEMENT RED FLAGS + NEWS" + ("  [TEST MODE]" if screener.TEST_MODE else ""))
    lines.append("=" * 84)
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    lines.append("\n--- MARKET HEADLINES ---")
    if market_headlines:
        for h in market_headlines:
            lines.append(f"  - {h}")
    else:
        lines.append("  (none fetched)")

    lines.append(f"\n--- TOP {len(top_results)} RANKED (valuation + statement checks + news) ---")
    for rank, r in enumerate(top_results, start=1):
        m = r["metrics"]
        price_str = f"${m['price']:.2f}" if m.get("price") else "n/a"
        daily_str = f"({m['daily_pct']:+.2f}% today)" if m.get("daily_pct") is not None else ""

        lines.append(f"\n#{rank}  {r['ticker']}  |  score: {r['score']}  |  {r['sector']}  |  {price_str} {daily_str}")

        lines.append("    Valuation vs. sector:")
        lines.extend(format_valuation_lines(r))

        lines.append("    Statement red-flag checks:")
        lines.extend(format_statement_lines(r.get("statement_checks")))

        lines.append("    Recent news:")
        lines.extend(format_news_lines(r.get("news")))

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

    print("\nFetching market headlines...")
    market_headlines = screener.get_market_headlines()

    scored = [r for r in all_results if r["score"] is not None]
    scored.sort(key=lambda r: r["score"], reverse=True)
    top_results = scored[:TOP_N]

    enrich_top_results(top_results)

    report = build_report(top_results, market_headlines)
    print("\n" + report)

    with open("combined_report.txt", "w") as f:
        f.write(report)
    print("Saved to combined_report.txt")

    subject = f"Combined Report - {datetime.now().strftime('%b %d')}"
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        print("[email] GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set -- skipping notification")
        return
    try:
        send_email_notification(subject, build_email_body(top_results))
    except Exception as e:
        print(f"[email] failed to send notification: {e}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        # Reuse screener.py's own test-mode switch for a fast end-to-end
        # smoke run instead of a real ~10-20 minute full S&P 500 scan.
        screener.TEST_MODE = True
        screener.TEST_TICKERS_PER_SECTOR = 2
        print("[--test] running with screener.TEST_MODE on (small ticker slice per sector)\n")

    main()
