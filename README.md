# S&P 500 Stock Research System

An automated equity-research pipeline that scans all ~500 S&P 500 companies, ranks them on relative value against their real GICS sector peers, checks the top picks' SEC filings for earnings-quality red flags, attaches recent news, and emails a summary. It runs on a schedule three times every trading day.

## What each component does

| Component | Purpose |
|---|---|
| **`screener/screener.py`** | Relative-valuation screener. Scores every S&P 500 stock against its GICS sector peers on six weighted metrics: PEG, EV/EBITDA, ROE, revenue growth, FCF yield and debt/equity. Outlier data such as a 4,000% ROE is clipped so it can't distort a whole sector's average. Also fetches per-ticker and market news with polite rate limiting. |
| **`statement_analyzer/statement_analyzer.py`** | Earnings-quality checks built on SEC EDGAR's XBRL `companyfacts` API. It flags three warning signs: receivables growing faster than revenue, net income outpacing operating cash flow, and sudden gross-margin jumps. It matches reporting periods so quarterly (10-Q) figures are never compared against annual (10-K) ones. |
| **`combined_report/combined_report.py`** | The orchestrator. Runs the full screener, takes the top 10, adds red-flag checks and news for those names, writes `combined_report.txt` and sends an email alert. It imports the other two modules directly, so there is no duplicated logic. |
| **`combined_report/growth_screener.py`** | A variant that applies a hard ≥10% revenue-growth filter before ranking. |
| **`market-data/sp500_constituents.csv`** | The single shared source for tickers, GICS sectors and SEC CIK numbers. |

## Tech used

- **Python 3**: standard library (`csv`, `smtplib`, `email`, `datetime`) plus `requests`
- **yfinance**: market prices, valuation ratios and news
- **SEC EDGAR API**: XBRL financial-statement data straight from company filings
- **Scheduled runs**: `cron` runs the pipeline at 9:35, 12:30 and 15:45 every weekday
- **Email alerts**: Gmail SMTP over SSL, with credentials read from environment variables

## How to run

```bash
git clone https://github.com/Hmelconian21/stock-research-system.git
cd stock-research-system
pip install -r requirements.txt

# Required by SEC EDGAR's fair-access policy (contact email in the User-Agent)
export SEC_USER_AGENT_EMAIL="you@example.com"

# Optional: email alerts (use a Gmail App Password, not your normal password)
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="your16charapppwd"
export NOTIFY_EMAIL="you@gmail.com"

cd combined_report
python3 combined_report.py --test   # quick smoke run on a small ticker slice
python3 combined_report.py          # full S&P 500 scan (~10-20 min)
```

Each script can also run on its own: `python3 screener/screener.py` or `python3 statement_analyzer/statement_analyzer.py`.

To schedule it, add a crontab entry like this:

```cron
35 9 * * 1-5 cd /path/to/stock-research-system/combined_report && python3 combined_report.py >> run_log.txt 2>&1
```

## Example output

An excerpt from `combined_report/combined_report.txt`:

```
====================================================================================
S&P 500 COMBINED REPORT: VALUATION + STATEMENT RED FLAGS + NEWS
====================================================================================

--- TOP 10 RANKED (valuation + statement checks + news) ---

#1  CHTR  |  score: 643.8  |  Communication Services  |  $110.48 (-0.80% today)
    Valuation vs. sector:
      PEG: 0.66  |  sector avg: 2.20  (+70.0% vs sector)
      EV/EBITDA: 5.25  |  sector avg: 14.70  (+64.3% vs sector)
      ROE: 0.27  |  sector avg: 0.36  (-25.3% vs sector)
      Revenue growth: -0.02  |  sector avg: 0.17  (-109.8% vs sector)
      FCF yield: 15.31  |  sector avg: 0.23  (+6505.9% vs sector)
      Debt/Equity: 441.58  |  sector avg: 146.80  (-200.8% vs sector)
    Statement red-flag checks:
      Receivables vs. revenue growth: OK
        revenue grew -1.4%, receivables grew 4.0% (gap: 5.4pp)
      Net income vs. operating cash flow: OK
        operating cash flow is 3.35x net income
      Gross margin jump: N/A
        insufficient data
```

Full sample outputs are in `combined_report/combined_report.txt`, `screener/screener_results.txt` and `statement_analyzer/statement_analysis_*.txt`.

## Engineering notes

- **Outlier clipping**: raw yfinance data sometimes contains extreme values. These are clipped before sector averages are computed so that one bad data point can't skew a whole sector.
- **Period matching**: SEC filings mix quarterly and annual values under the same XBRL tag. Duration facts are filtered so that only periods of the same length are compared, which fixed false readings such as NVDA showing −62% revenue "growth".
- **Single source of truth**: all modules read one shared constituents file. An earlier version kept a separate copy per script, and the copies drifted into different schemas.
- **No secrets in code**: credentials come only from environment variables.

*This is a personal research tool and not investment advice.*
