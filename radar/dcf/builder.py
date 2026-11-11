# DCF engine (vendored from the author's earlier valuation tool).
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# dcf_builder.py  美股单只股票 DCF 估值工具
# A lightweight DCF builder that reproduces two valuation tables similar to your spreadsheets.
# It uses SEC EDGAR companyfacts (XBRL) to pull Revenue/Net Income/CFO/CapEx, computes FCF and margins,
# adopts analyst revenue growth via yfinance if available (optional), otherwise falls back to a 3Y CAGR.
# Then it projects 4 years of FCF and discounts them plus a terminal value.
#
# Usage example:
#   python dcf_builder.py --ticker GOOGL --required 0.07 --perp 0.025 --avg-years 5
#
# Outputs in current folder:
#   <YEAR>_dcf_<TICKER>.csv            # historical table
#   <YEAR>_dcf_<TICKER>_proj.csv       # projection + DCF table
#   <YEAR>_dcf_<TICKER>_meta.json      # summary (fair value, assumptions)

# 数据来源
# 	•	SEC EDGAR companyfacts API
# 	•	先通过 https://www.sec.gov/files/company_tickers.json 找到公司对应的 CIK
# 	•	再通过 https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json 拉取 XBRL 明细
# 	•	可选：yfinance 分析师预测
# 	•	若可用，使用 yfinance 的 earnings_forecasts 里的 revenue growth 作为未来增长率
# 	•	否则回退到最近 3–5 年的 Revenue CAGR
# 标准 10 年三阶段 DCF 模型
# 	•	第 1 阶段：快速增长期（Years 1–5）
# 	•	第 2 阶段：稳定期（Years 6–10）
# 	•	第 3 阶段：永续增长（Perpetual）

import argparse, json, math, re, sys
from typing import Dict, Tuple, Optional
import pandas as pd
import numpy as np
import requests
from pprint import pprint
from typing import Dict, Tuple, Optional, List

from radar.dcf.dcf_utils import get_latest_price_stooq, map_sic_to_sector,infer_sector_from_facts,is_true_annual_period



"""
# =======================================
        Module organization
# =======================================
This file is organized into three logical sections to make it easier to read and
maintain:

  1) Fetch layer: functions that call external services (SEC, Stooq) and
      flatten companyfacts JSON (`fetch_cik`, `fetch_companyfacts`,
      `companyfacts_to_table`, `fetch_company_data`).

  2) Decision layer: helpers that parse XBRL tags/periods and choose which
      series/tags to use (`_series_from_tags`, `extract_financials`,
      `adopt_growth_rate`, `infer_sector_from_facts` in dcf_utils).

  3) Calculation layer: pure-ish functions that compute margins, fcf margins,
      growth paths and perform DCF math (`compute_margins`,
      `compute_fcf_margin`, `build_growth_curve`, `build_dcf`).

The goal of this minimal refactor is to separate responsibilities and make the
main `build_dcf` flow easier to follow.
"""

# =======================================
# 数据自定义区
# =======================================
import os as _os
from radar.http import user_agent as _ua
SEC_HEADERS = {"User-Agent": _ua()}  # SEC wants a real contact (SEC_USER_AGENT in .env)
SECTOR_GROWTH = {
    "TECH":      {"fast": (0.12, 0.25), "stable": (0.05, 0.10), "terminal": (0.02, 0.03)},
    "CONSUMER":  {"fast": (0.03, 0.10), "stable": (0.02, 0.04), "terminal": (0.015, 0.025)},
    "BANK":      {"fast": (0.02, 0.06), "stable": (0.01, 0.04), "terminal": (0.01, 0.02)},
    "INSURANCE": {"fast": (0.00, 0.05), "stable": (0.00, 0.03), "terminal": (0.01, 0.02)},
    "ENERGY":    {"fast": (-0.03, 0.08), "stable": (0.01, 0.03), "terminal": (0.01, 0.02)},
    "PHARMA":    {"fast": (0.05, 0.12), "stable": (0.03, 0.06), "terminal": (0.02, 0.02)},
}
# yfinance fallback tag names — appended at the end of the *_TAGS priority
# lists below so SEC-reported values always win when present. See the
# YFINANCE_FIELD_MAP block further down for what each one maps to.
YF_REVENUE = "YF_Revenue"
YF_NET_INCOME = "YF_NetIncome"
YF_GROSS_PROFIT = "YF_GrossProfit"
YF_OPERATING_INCOME = "YF_OperatingIncome"
YF_INTEREST_EXPENSE = "YF_InterestExpense"
YF_INCOME_TAX = "YF_IncomeTax"
YF_INCOME_BEFORE_TAX = "YF_IncomeBeforeTax"
YF_RD_EXPENSE = "YF_RDExpense"
YF_EPS_DILUTED = "YF_EPSDiluted"
YF_CFO = "YF_CFO"
YF_CAPEX = "YF_CapEx"
YF_DA = "YF_DA"
YF_CASH = "YF_Cash"
YF_LONG_TERM_DEBT = "YF_LongTermDebt"
YF_SHORT_TERM_DEBT = "YF_ShortTermDebt"
YF_TOTAL_ASSETS = "YF_TotalAssets"
YF_STOCKHOLDERS_EQUITY = "YF_StockholdersEquity"

REVENUE_TAGS = [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueServicesNet",
        "SalesRevenueGoodsNet",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "ContractRevenue",
        "ServiceRevenue",
        "OperatingRevenue",
        "OilAndGasRevenue",
        "ElectricUtilityRevenue",
        "RealEstateRevenue",
        "FinancialServicesRevenue",
        "InterestAndDividendIncomeOperating",
        "PremiumsEarned",
        "NetInvestmentIncome",
        "Revenue",                             # ifrs-full (20-F/40-F filers, e.g. TSM)
        "RevenueFromContractsWithCustomers",   # ifrs-full alt tag
        YF_REVENUE,
]
NET_INCOME_TAGS = [
    "NetIncomeLoss",
    "ProfitLoss",   # also the ifrs-full tag name
    YF_NET_INCOME,
]
CFO_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    "CashFlowsFromUsedInOperatingActivities",  # ifrs-full
    YF_CFO,
]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "CapitalExpendituresInvestments",
    "PaymentsToAcquireProductiveAssets",
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",  # ifrs-full
    YF_CAPEX,
]
SHARE_OUTSTANDING_TAGS = [
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "CommonStockSharesOutstanding",
    "EntityCommonStockSharesOutstanding",
    # deliberately no YF_ fallback tag here — see note by YFINANCE_FIELD_MAP
]
GROSS_PROFIT_TAGS = [
    "GrossProfit",   # also the ifrs-full tag name
    YF_GROSS_PROFIT,
]
EPS_DILUTED_TAGS = [
    "EarningsPerShareDiluted",
    "EarningsPerShareBasicAndDiluted",
    "DilutedEarningsLossPerShare",  # ifrs-full
    YF_EPS_DILUTED,
]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsAndShortTermInvestments",
    "Cash",
    YF_CASH,
]
MARKETABLE_SECURITIES_CURRENT_TAGS = [
    "MarketableSecuritiesCurrent",
]
MARKETABLE_SECURITIES_NONCURRENT_TAGS = [
    "MarketableSecuritiesNoncurrent",
]
LONG_TERM_DEBT_TAGS = [
    "LongTermDebt",
    "LongTermDebtNoncurrent",
    "LongTermDebtAndCapitalLeaseObligations",
    YF_LONG_TERM_DEBT,
]
SHORT_TERM_DEBT_TAGS = [
    "ShortTermBorrowings",
    "CommercialPaper",
    "LongTermDebtCurrent",
    "DebtCurrent",
    YF_SHORT_TERM_DEBT,
]
TOTAL_ASSETS_TAGS = [
    "Assets",
    YF_TOTAL_ASSETS,
]
STOCKHOLDERS_EQUITY_TAGS = [
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    YF_STOCKHOLDERS_EQUITY,
]
OPERATING_INCOME_TAGS = [
    "OperatingIncomeLoss",
    "ProfitLossFromOperatingActivities",  # ifrs-full
    YF_OPERATING_INCOME,
]
# ifrs-full has no single "total D&A" tag — it's split across these two.
# Summed into the synthetic IFRS_DA_TOTAL tag by _sum_ifrs_tags_into_synthetic()
# in fetch_companyfacts(), which is what DA_TAGS actually references below.
DA_COMPONENT_TAGS = ["DepreciationExpense", "AmortisationExpense"]
IFRS_DA_TOTAL = "IFRS_DA_Total"
DA_TAGS = [
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "Depreciation",
    IFRS_DA_TOTAL,
    YF_DA,
]
# ifrs-full also has no single "total interest expense" tag — it's split by
# liability type. Summed into IFRS_INTEREST_EXPENSE_TOTAL the same way as D&A.
INTEREST_EXPENSE_COMPONENT_TAGS = [
    "InterestExpenseOnBonds",
    "InterestExpenseOnBorrowings",
    "InterestExpenseOnFinanceLeases",
    "InterestExpenseOnLeaseLiabilities",
    "InterestExpenseOnOtherFinancialLiabilities",
]
IFRS_INTEREST_EXPENSE_TOTAL = "IFRS_InterestExpense_Total"
INTEREST_EXPENSE_TAGS = [
    "InterestExpense",
    "InterestExpenseDebt",
    IFRS_INTEREST_EXPENSE_TOTAL,
    YF_INTEREST_EXPENSE,
]
INCOME_TAX_TAGS = [
    "IncomeTaxExpenseBenefit",
    "IncomeTaxExpenseContinuingOperations",  # ifrs-full
    YF_INCOME_TAX,
]
INCOME_BEFORE_TAX_TAGS = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    YF_INCOME_BEFORE_TAX,
]
DIVIDENDS_PER_SHARE_TAGS = [
    "CommonStockDividendsPerShareDeclared",
    "CommonStockDividendsPerShareCashPaid",
]
RD_EXPENSE_TAGS = [
    "ResearchAndDevelopmentExpense",  # also the ifrs-full tag name
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    YF_RD_EXPENSE,
]
SBC_TAGS = [
    "ShareBasedCompensation",
    "AllocatedShareBasedCompensationExpense",
]
BUYBACK_TAGS = [
    "PaymentsForRepurchaseOfCommonStock",
    "PaymentsForRepurchaseOfEquity",
]
DIVIDENDS_PAID_TAGS = [
    "PaymentsOfDividendsCommonStock",
    "PaymentsOfDividends",
    "PaymentsOfOrdinaryDividends",
]
ACCOUNTS_RECEIVABLE_TAGS = [
    "AccountsReceivableNetCurrent",
    "AccountsReceivableNet",
    "ReceivablesNetCurrent",
]

# -----------------------------------------------
#  yfinance fallback tags (used only when SEC XBRL has no us-gaap data for
#  the ticker — e.g. foreign private issuers filing 20-F/40-F under IFRS,
#  whose SEC XBRL lives under the "ifrs-full" namespace instead of
#  "us-gaap"). Kept as a synthetic "yfinance" namespace merged into
#  companyfacts by fetch_companyfacts(), and appended at the END of each
#  *_TAGS priority list below so real SEC-reported values always win when
#  present. (YF_* name constants themselves are defined above REVENUE_TAGS
#  since the *_TAGS lists reference them.)
# -----------------------------------------------
# field -> (yfinance statement, yfinance row label, sign, unit, synthetic tag)
# sign flips yfinance's convention to match SEC's (e.g. yfinance reports
# CapEx as a negative outflow; SEC's PaymentsToAcquirePropertyPlantAndEquipment
# is a positive cash-paid amount).
YFINANCE_FIELD_MAP = {
    "revenue":             ("income",   "Total Revenue",                1, "USD", YF_REVENUE),
    "net_income":          ("income",   "Net Income",                   1, "USD", YF_NET_INCOME),
    "gross_profit":        ("income",   "Gross Profit",                 1, "USD", YF_GROSS_PROFIT),
    "operating_income":    ("income",   "Operating Income",             1, "USD", YF_OPERATING_INCOME),
    "interest_expense":    ("income",   "Interest Expense",             1, "USD", YF_INTEREST_EXPENSE),
    "income_tax":          ("income",   "Tax Provision",                1, "USD", YF_INCOME_TAX),
    "income_before_tax":   ("income",   "Pretax Income",                1, "USD", YF_INCOME_BEFORE_TAX),
    "rd_expense":          ("income",   "Research And Development",     1, "USD", YF_RD_EXPENSE),
    "eps_diluted":         ("income",   "Diluted EPS",                  1, "USD/shares", YF_EPS_DILUTED),
    "cfo":                 ("cashflow", "Operating Cash Flow",          1, "USD", YF_CFO),
    "capex":               ("cashflow", "Capital Expenditure",         -1, "USD", YF_CAPEX),
    "da":                  ("cashflow", "Depreciation And Amortization", 1, "USD", YF_DA),
    "cash":                ("balance",  "Cash And Cash Equivalents",    1, "USD", YF_CASH),
    "long_term_debt":      ("balance",  "Long Term Debt",               1, "USD", YF_LONG_TERM_DEBT),
    "short_term_debt":     ("balance",  "Current Debt",                 1, "USD", YF_SHORT_TERM_DEBT),
    "total_assets":        ("balance",  "Total Assets",                 1, "USD", YF_TOTAL_ASSETS),
    "stockholders_equity": ("balance",  "Stockholders Equity",          1, "USD", YF_STOCKHOLDERS_EQUITY),
    # NOTE: "shares_outstanding" is deliberately NOT mapped here. yfinance's
    # balance-sheet "Ordinary Shares Number" is on an ordinary-share basis
    # (e.g. ~25.9B for TSM), while the rest of this script — and the
    # existing SEC-has-no-shares-data fallback a few hundred lines down in
    # build_dcf() — uses info()["sharesOutstanding"], which for ADRs is on
    # the traded-security (ADS) basis (~5.19B for TSM, since 1 ADS = 5
    # ordinary shares). Mapping it here would populate a non-empty (but
    # wrong-basis, ~5x off) series that silently short-circuits that
    # already-correct fallback.
}


# =======================================
#  Fetch layer: external I/O functions
# =======================================

# Manual CIK overrides for tickers missing from SEC's public ticker JSONs.
# Verify via https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<TICKER>
KNOWN_CIKS = {
    "HOLX": "0000859737",  # Hologic Inc — absent from both SEC ticker files
}


# Tickers whose current SEC entity has no 10-K history yet (reorganised into a new holding company):
# use the predecessor registrant, which holds the annual reports. (invest-radar addition)
PREDECESSOR_CIKS = {
    "XOM": "0000034088",  # Exxon Mobil Corp → ExxonMobil Holdings Corp (2026)
}


# ticker -》 cik
def fetch_cik(ticker: str) -> str:
    ticker_u = ticker.upper()
    if ticker_u in PREDECESSOR_CIKS:
        return PREDECESSOR_CIKS[ticker_u]

    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    for _, v in r.json().items():
        if v.get("ticker", "").upper() == ticker_u:
            return str(v["cik_str"]).zfill(10)

    r = requests.get("https://www.sec.gov/files/company_tickers_exchange.json",
                     headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    payload = r.json()
    fields = payload.get("fields", [])
    if "ticker" in fields and "cik" in fields:
        ti, ci = fields.index("ticker"), fields.index("cik")
        for row in payload.get("data", []):
            if str(row[ti]).upper() == ticker_u:
                return str(row[ci]).zfill(10)

    if ticker_u in KNOWN_CIKS:
        return KNOWN_CIKS[ticker_u]

    raise ValueError(f"Unable to find CIK for ticker {ticker}")

# ticker -> companyfacts JSON
def fetch_companyfacts(cik: str, ticker: Optional[str] = None) -> dict:
    print(f"Fetching companyfacts for CIK {cik}...")
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    r = requests.get(url, headers=SEC_HEADERS, timeout=60)
    r.raise_for_status()
    facts = r.json()

    # Foreign private issuers (20-F/40-F filers) often report XBRL under the
    # "ifrs-full" taxonomy instead of "us-gaap". When that's the case:
    #   1) SEC's own USD convenience-translation figures under ifrs-full are
    #      used directly wherever _series_from_tags/_quarterly_series_from_tags
    #      look them up (see the tag lists above, e.g. "Revenue", "ProfitLoss").
    #   2) Older comparative years (reported only in local currency, not yet
    #      given a USD translation by SEC) are backfilled to USD via spot FX.
    #   3) A few fields ifrs-full splits into multiple line items with no
    #      single "total" tag (D&A, interest expense) get summed into a
    #      synthetic total tag.
    #   4) yfinance financial statements remain the final fallback for
    #      anything ifrs-full still doesn't cover (e.g. share counts, SBC),
    #      merged in as a synthetic "yfinance" namespace.
    _IFRS_ONLY_REVENUE_TAGS = {"Revenue", "RevenueFromContractsWithCustomers"}
    if ticker:
        us_gaap_tags = facts.get("facts", {}).get("us-gaap", {})
        has_revenue = any(
            t in us_gaap_tags for t in REVENUE_TAGS
            if not t.startswith("YF_") and t not in _IFRS_ONLY_REVENUE_TAGS
        )
        if not has_revenue:
            ifrs_full_tags = facts.get("facts", {}).get("ifrs-full")
            if ifrs_full_tags:
                try:
                    backfill_ifrs_full_usd(facts)
                    _sum_ifrs_tags_into_synthetic(facts, DA_COMPONENT_TAGS, IFRS_DA_TOTAL)
                    _sum_ifrs_tags_into_synthetic(facts, INTEREST_EXPENSE_COMPONENT_TAGS, IFRS_INTEREST_EXPENSE_TOTAL)
                except Exception as e:
                    print(f"  [ifrs-full backfill] failed for {ticker}: {e}")

            try:
                supplement = fetch_yfinance_supplement(ticker)
            except Exception as e:
                print(f"  [yfinance fallback] failed for {ticker}: {e}")
                supplement = {}
            if supplement:
                print(f"  [yfinance fallback] SEC has no us-gaap revenue data for {ticker}; "
                      f"using yfinance financial statements instead ({len(supplement)} tags).")
                facts.setdefault("facts", {})["yfinance"] = supplement

    return facts


def _yfinance_fx_to_usd(currency: str, dates: list) -> dict:
    """Map each date in `dates` to a currency->USD multiplier, using Yahoo's
    "<CUR>USD=X" quote (USD price of 1 unit of `currency`). Returns 1.0 for
    every date if `currency` is already USD or the FX history can't be
    fetched (better to show foreign-currency-scale numbers than to crash).
    """
    dates = sorted(set(pd.Timestamp(d).normalize() for d in dates if d is not None))
    if not currency or currency == "USD" or not dates:
        return {d: 1.0 for d in dates}

    import yfinance as yf
    try:
        fx_hist = yf.Ticker(f"{currency}USD=X").history(
            start=(dates[0] - pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
            end=(dates[-1] + pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
        )
    except Exception:
        fx_hist = None

    if fx_hist is None or fx_hist.empty:
        return {d: 1.0 for d in dates}

    close = fx_hist["Close"]
    close.index = pd.to_datetime(close.index)
    if close.index.tz is not None:
        close.index = close.index.tz_localize(None)
    close = close.sort_index()

    result = {}
    for d in dates:
        asof = close.index[close.index <= d]
        result[d] = float(close.loc[asof[-1]]) if len(asof) else float(close.iloc[0])
    return result


def fetch_yfinance_supplement(ticker: str) -> dict:
    """Build a synthetic companyfacts-shaped block (annual + quarterly) from
    yfinance's financial statements, for tickers whose SEC XBRL has no
    us-gaap data (foreign private issuers reporting under IFRS).

    Converts to USD when the filer reports in another currency (e.g. TSM
    reports in TWD) using the historical <CUR>USD=X rate as of each period
    end, so revenue growth / margins reflect real trends rather than FX
    drift, and so figures are comparable to the USD-quoted share price used
    elsewhere in this script.

    Shape matches facts["facts"][<namespace>][<tag>]["units"][<unit>] = [items],
    so it plugs directly into _series_from_tags / _quarterly_series_from_tags.
    """
    import yfinance as yf

    tk = yf.Ticker(ticker)
    currency = (tk.info or {}).get("financialCurrency") or "USD"

    statements = {
        "income":   {"annual": tk.financials,   "quarterly": tk.quarterly_financials},
        "balance":  {"annual": tk.balance_sheet, "quarterly": tk.quarterly_balance_sheet},
        "cashflow": {"annual": tk.cashflow,      "quarterly": tk.quarterly_cashflow},
    }

    # Collect every period-end date across all statements up front so we
    # only hit the FX endpoint once per ticker.
    all_dates = []
    for stmt in statements.values():
        for df in stmt.values():
            if df is not None and not df.empty:
                all_dates.extend(df.columns.tolist())
    fx_map = _yfinance_fx_to_usd(currency, all_dates)

    def _fx(ts) -> float:
        return fx_map.get(pd.Timestamp(ts).normalize(), 1.0)

    yfinance_facts: dict = {}

    for field, (stmt, row_label, sign, unit, tag) in YFINANCE_FIELD_MAP.items():
        df_annual = statements[stmt]["annual"]
        df_quarterly = statements[stmt]["quarterly"]
        items = []

        if df_annual is not None and row_label in df_annual.index:
            for end_ts, val in df_annual.loc[row_label].items():
                if pd.isna(val):
                    continue
                year = end_ts.year
                conv = 1.0 if unit == "shares" else _fx(end_ts)
                items.append({
                    "start": f"{year}-01-01",
                    "end": end_ts.strftime("%Y-%m-%d"),
                    "val": float(val) * sign * conv,
                    "form": "10-K",
                    "fp": "FY",
                    "fy": year,
                    "frame": f"FY{year}",
                })

        if df_quarterly is not None and row_label in df_quarterly.index:
            for end_ts, val in df_quarterly.loc[row_label].items():
                if pd.isna(val):
                    continue
                year = end_ts.year
                q = (end_ts.month - 1) // 3 + 1
                conv = 1.0 if unit == "shares" else _fx(end_ts)
                v = float(val) * sign * conv
                start_ts = end_ts - pd.DateOffset(months=3) + pd.Timedelta(days=1)
                # Duration-style item (matches the quarterly extractor's
                # is_instant=False lookup, e.g. revenue/net income/CFO).
                items.append({
                    "start": start_ts.strftime("%Y-%m-%d"),
                    "end": end_ts.strftime("%Y-%m-%d"),
                    "val": v,
                    "form": "10-Q",
                    "fp": f"Q{q}",
                    "fy": year,
                    "frame": f"CY{year}Q{q}",
                })
                # Instant-style item (matches is_instant=True lookup used for
                # balance-sheet fields like cash/debt/assets/equity).
                items.append({
                    "start": end_ts.strftime("%Y-%m-%d"),
                    "end": end_ts.strftime("%Y-%m-%d"),
                    "val": v,
                    "form": "10-Q",
                    "fp": f"Q{q}",
                    "fy": year,
                    "frame": f"CY{year}Q{q}I",
                })

        if items:
            yfinance_facts[tag] = {"units": {unit: items}}

    return yfinance_facts


_ANNUAL_FORMS = ("10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A")
_LOCAL_CURRENCY_UNIT_RE = re.compile(r"^([A-Z]{3})(/shares)?$")


def _annual_items_by_year(items: list) -> Dict[int, dict]:
    """Reduce a raw XBRL items list down to one annual item per fiscal year
    (form in _ANNUAL_FORMS, fp FY/CY/blank, year resolved the same way
    _series_from_tags resolves it). Later items in the list win ties, matching
    _series_from_tags' `drop_duplicates(..., keep="last")` convention.
    """
    out: Dict[int, dict] = {}
    for it in items:
        if it.get("form") not in _ANNUAL_FORMS:
            continue
        if (it.get("fp") or "").upper() not in ("FY", "CY", ""):
            continue
        year = parse_fiscal_year_from_item(it)
        if year is not None:
            out[year] = it
    return out


def _local_currency_unit_for(units: dict) -> Optional[Tuple[str, str, bool]]:
    """Find a non-USD ISO-currency unit key in an XBRL tag's `units` dict
    (e.g. "TWD" or "TWD/shares"). Returns (unit_key, currency_code, per_share).
    """
    for unit_key in units:
        m = _LOCAL_CURRENCY_UNIT_RE.fullmatch(unit_key)
        if m and m.group(1) != "USD":
            return unit_key, m.group(1), bool(m.group(2))
    return None


def backfill_ifrs_full_usd(facts: dict) -> None:
    """Fill in missing USD figures for ifrs-full tags, for foreign private
    issuers (20-F/40-F) whose SEC XBRL reports both a local-currency amount
    and a USD convenience translation.

    The USD translation is typically only present for years covered by the
    *current* filing's face financials — older comparative years carried over
    from a prior filing (e.g. TSM's FY2016 figures, which only appear as the
    prior-year column in the FY2017 20-F) exist in SEC's data solely in the
    local currency. This backfills those years by converting the
    local-currency value to USD using the historical spot FX rate as of the
    period end (via `_yfinance_fx_to_usd`), so a USD-only lookup (as done by
    `_series_from_tags`) sees a complete series instead of stopping wherever
    SEC's own USD translation happens to start.

    Note this is a spot-rate approximation — SEC's own translations may use a
    period-average rate — consistent with the approximation already used for
    the yfinance fallback elsewhere in this file. Mutates `facts` in place.
    """
    ifrs_full = facts.get("facts", {}).get("ifrs-full")
    if not ifrs_full:
        return

    for tag_info in ifrs_full.values():
        units = tag_info.get("units", {})
        local = _local_currency_unit_for(units)
        if not local:
            continue
        local_unit, currency, per_share = local
        usd_unit = "USD/shares" if per_share else "USD"

        local_by_year = _annual_items_by_year(units.get(local_unit, []))
        usd_by_year = _annual_items_by_year(units.get(usd_unit, []))
        missing_years = sorted(y for y in local_by_year if y not in usd_by_year)
        if not missing_years:
            continue

        dates = [pd.Timestamp(local_by_year[y]["end"]) for y in missing_years]
        fx_map = _yfinance_fx_to_usd(currency, dates)

        usd_items = units.setdefault(usd_unit, [])
        for year in missing_years:
            src = local_by_year[year]
            try:
                local_val = float(src["val"])
            except Exception:
                continue
            end_ts = pd.Timestamp(src["end"]).normalize()
            rate = fx_map.get(end_ts, 1.0)
            usd_items.append({**src, "val": local_val * rate})


def _sum_ifrs_tags_into_synthetic(facts: dict, source_tags: List[str], synthetic_tag: str, unit: str = "USD") -> None:
    """ifrs-full sometimes splits a field this script treats as one line item
    (e.g. total D&A, total interest expense) across several component tags
    with no single "total" tag. This sums `source_tags` per fiscal year and
    writes the result into the ifrs-full namespace under `synthetic_tag`, so
    it can be referenced like any other tag from the *_TAGS priority lists.
    Call this AFTER `backfill_ifrs_full_usd` so the sum covers backfilled
    early years too. Mutates `facts` in place.
    """
    ifrs_full = facts.get("facts", {}).get("ifrs-full")
    if not ifrs_full:
        return

    per_tag_year = {}
    for tag in source_tags:
        info = ifrs_full.get(tag)
        if not info:
            continue
        per_tag_year[tag] = _annual_items_by_year(info.get("units", {}).get(unit, []))
    if not per_tag_year:
        return

    all_years = sorted(set().union(*(d.keys() for d in per_tag_year.values())))
    synth_items = []
    for year in all_years:
        total = 0.0
        template = None
        for year_map in per_tag_year.values():
            it = year_map.get(year)
            if it is None:
                continue
            try:
                total += float(it["val"])
            except Exception:
                continue
            template = template or it
        if template is not None:
            synth_items.append({**template, "val": total})

    if synth_items:
        ifrs_full[synthetic_tag] = {"units": {unit: synth_items}}


# companyfacts JSON -> pandas DataFrame for further processing
def companyfacts_to_table(facts: dict) -> pd.DataFrame:
    """
    将 SEC companyfacts JSON 展平为一个 pandas DataFrame。
    每一行 = 某个 tag 在某个 unit 下的一条披露记录。
    """
    cik = facts.get("cik")
    entity_name = facts.get("entityName")
    facts_block = facts.get("facts", {})

    rows = []

    for taxonomy, tags in facts_block.items():          # 比如 "us-gaap", "dei"
        for tag, info in tags.items():                  # 比如 "Revenues", "NetIncomeLoss"
            label = info.get("label")
            units = info.get("units", {})

            for unit, items in units.items():           # 比如 "USD", "shares"
                for it in items:                        # 每条披露记录
                    rows.append({
                        "cik": cik,
                        "entityName": entity_name,
                        "taxonomy": taxonomy,
                        "tag": tag,
                        "label": label,
                        "unit": unit,
                        "val": it.get("val"),
                        "fy": it.get("fy"),
                        "fp": it.get("fp"),
                        "form": it.get("form"),
                        "start": it.get("start"),
                        "end": it.get("end"),
                        "filed": it.get("filed"),
                        "accn": it.get("accn"),
                        "frame": it.get("frame"),
                    })

    df = pd.DataFrame(rows)
    df = df.sort_values(
        ["taxonomy", "tag", "fy", "fp", "form", "filed"],
        ignore_index=True
    )
    return df

#   calling function 
def fetch_company_data(ticker: str):
    """High level fetch wrapper.

    Returns (cik, facts) for the given ticker. Keeping this wrapper lets the
    build_dcf flow call a single function for external I/O and makes testing
    easier (mock this function in tests).
    """
    cik = fetch_cik(ticker)
    facts = fetch_companyfacts(cik, ticker=ticker)
    return cik, facts

# =======================================
#  Decision layer: tag/period selection helpers
# =======================================

#   extract shares outstanding time series
def extract_shares_outstanding(facts):
    share_outstanding_tags = SHARE_OUTSTANDING_TAGS
    s, _tag, _periods = _series_from_tags(facts, share_outstanding_tags, unit="shares")
    return s.sort_index()

#   extract all financial time series into a dict
def extract_all_financials(facts: dict) -> dict:
    """Extract all financial time series from companyfacts.

    Returns dict mapping metric name to (series, tag, periods) tuple.
    """
    result = {}

    # USD metrics (income statement / cash flow / balance sheet)
    for name, tags in [
        ("revenue", REVENUE_TAGS),
        ("net_income", NET_INCOME_TAGS),
        ("cfo", CFO_TAGS),
        ("capex", CAPEX_TAGS),
        ("gross_profit", GROSS_PROFIT_TAGS),
        ("operating_income", OPERATING_INCOME_TAGS),
        ("da", DA_TAGS),
        ("interest_expense", INTEREST_EXPENSE_TAGS),
        ("income_tax", INCOME_TAX_TAGS),
        ("income_before_tax", INCOME_BEFORE_TAX_TAGS),
        ("rd_expense", RD_EXPENSE_TAGS),
        ("sbc", SBC_TAGS),
        ("buyback", BUYBACK_TAGS),
        ("dividends_paid", DIVIDENDS_PAID_TAGS),
        ("cash", CASH_TAGS),
        ("marketable_securities_current", MARKETABLE_SECURITIES_CURRENT_TAGS),
        ("marketable_securities_noncurrent", MARKETABLE_SECURITIES_NONCURRENT_TAGS),
        ("long_term_debt", LONG_TERM_DEBT_TAGS),
        ("short_term_debt", SHORT_TERM_DEBT_TAGS),
        ("total_assets", TOTAL_ASSETS_TAGS),
        ("stockholders_equity", STOCKHOLDERS_EQUITY_TAGS),
        ("accounts_receivable", ACCOUNTS_RECEIVABLE_TAGS),
    ]:
        s, tag, periods = _series_from_tags(facts, tags, unit="USD")
        result[name] = (s, tag, periods)

    # Per-share metrics (USD/shares)
    for name, tags in [
        ("eps_diluted", EPS_DILUTED_TAGS),
        ("dividends_per_share", DIVIDENDS_PER_SHARE_TAGS),
    ]:
        s, tag, periods = _series_from_tags(facts, tags, unit="USD/shares")
        result[name] = (s, tag, periods)

    # Share counts (shares)
    s, tag, periods = _series_from_tags(facts, SHARE_OUTSTANDING_TAGS, unit="shares")
    result["shares_outstanding"] = (s, tag, periods)

    # Derived: FCF = CFO - CapEx
    cfo = result["cfo"][0]
    capex = result["capex"][0]
    idx = cfo.index.union(capex.index)
    fcf = cfo.reindex(idx) - capex.reindex(idx)
    fcf.name = "FreeCashFlow"
    result["fcf"] = (fcf, None, {})

    # Derived: Total Debt = Long Term + Short Term
    ltd = result["long_term_debt"][0]
    std = result["short_term_debt"][0]
    idx = ltd.index.union(std.index)
    total_debt = ltd.reindex(idx).fillna(0) + std.reindex(idx).fillna(0)
    total_debt.name = "TotalDebt"
    result["total_debt"] = (total_debt, None, {})

    # Derived: EBITDA = Operating Income + D&A
    oi = result["operating_income"][0]
    da = result["da"][0]
    idx = oi.index.union(da.index)
    ebitda = oi.reindex(idx).fillna(0) + da.reindex(idx).fillna(0)
    ebitda.name = "EBITDA"
    result["ebitda"] = (ebitda, None, {})

    # Derived: Total Cash = Cash & Equiv + Marketable Securities (Current + Noncurrent)
    c = result["cash"][0]
    ms_cur = result["marketable_securities_current"][0]
    ms_nc = result["marketable_securities_noncurrent"][0]
    idx = c.index.union(ms_cur.index).union(ms_nc.index)
    total_cash = c.reindex(idx).fillna(0) + ms_cur.reindex(idx).fillna(0) + ms_nc.reindex(idx).fillna(0)
    total_cash.name = "TotalCash"
    result["total_cash"] = (total_cash, None, {})

    return result


#   extract financial time series — backward-compatible wrapper
def extract_financials(facts: dict):
    """Original 11-tuple interface for backward compatibility with build_dcf / dcf_screener."""
    data = extract_all_financials(facts)

    revenue, revenue_tag, revenue_periods = data["revenue"]
    net_income, net_income_tag, net_income_periods = data["net_income"]
    _, cfo_tag, cfo_periods = data["cfo"]
    _, capex_tag, capex_periods = data["capex"]
    fcf = data["fcf"][0]

    return (
        revenue,
        net_income,
        fcf,
        revenue_tag,
        net_income_tag,
        cfo_tag,
        capex_tag,
        revenue_periods,
        net_income_periods,
        cfo_periods,
        capex_periods,
    )

#  Helper Function: extract "年度年份" from single XBRL item
def parse_fiscal_year_from_item(item: dict) -> Optional[int]:
    """
    从单条 XBRL 披露记录里，尽量安全地解析出“年度年份”。
    只在我们确认它是“年度数据”的时候返回 year（int），否则返回 None。
    判断顺序：
      1) frame 是年度形式：CY2022 / FY2022 -> 直接取 2022
      2) 若有 start / end 且 is_true_annual_period(start, end) 为 True，则用 end.year
      3) 最后兜底用 fy
    """
    from pandas import to_datetime
    import re

    frame = item.get("frame")
    start = item.get("start")
    end = item.get("end")
    fy = item.get("fy")

    # 1) 优先：frame 是严格的年度形式 (CY2022 / FY2022)
    if frame:
        s = str(frame)
        m = re.fullmatch(r"(CY|FY)(19|20)\d{2}", s)
        if m:
            # 例如 CY2022 -> 取最后 4 位
            return int(s[-4:])

        # frame 存在但不是年度形式（如 CY2022Q1）→ 明确是季度，直接拒绝
        # 不再 fallback 到 fy，防止季度数据冒充年度
        return None

    # 2) 次优：有 start / end，并且通过 is_true_annual_period 判定为"真·年度"
    if start and end:
        try:
            if is_true_annual_period(start, end):
                return int(to_datetime(end).year)
        except Exception:
            pass
        # start/end 存在但不是年度区间 → 拒绝，不 fallback 到 fy
        return None

    # 3) 最后兜底：没有 frame 也没有 start/end 时，退回 fy
    if fy is not None:
        try:
            return int(fy)
        except Exception:
            pass

    # 实在不确定，就返回 None，调用方自己丢弃这一条
    return None

#  Helper Function: extract entities/series from multiple possible tags 
def _series_from_tags(facts: dict, tags: List[str], unit: str = "USD") -> Tuple[pd.Series, Optional[str], dict]:
    import re

    all_series = [] # 记录所有可用的 series
    tag_names = [] # 记录对应的 tag 名称
    # per-tag/year periods: { tag: { year: (start,end) }}
    per_tag_periods = {}
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    ifrs_full = facts.get("facts", {}).get("ifrs-full", {})  # 20-F/40-F filers, see backfill_ifrs_full_usd
    yfinance_ns = facts.get("facts", {}).get("yfinance", {})  # synthetic fallback, see fetch_companyfacts

    for tag in tags:
        tag_info = us_gaap.get(tag) or ifrs_full.get(tag) or yfinance_ns.get(tag) # 某个 tag 的信息
        if not tag_info:
            continue

        units = tag_info.get("units", {}) # 各个单位下的披露数据
        if unit not in units:
            continue

        rows = []
        periods = {}
        for item in units[unit]:
            form = item.get("form", "")
            fp = (item.get("fp") or "").upper()
            start = item.get("start")
            end = item.get("end")

            # 只要年报的 Form（含修正版 /A）
            if form not in ("10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A"):
                continue

            # fp 一般是 FY / CY / Q1 等，这里可以保守一点：
            if fp not in ("FY", "CY", ""):
                continue

            # 统一用 helper 来解析“年度年份”
            year = parse_fiscal_year_from_item(item)
            if year is None:
                # 这条记录要么是季度，要么期间不完整，就当作不是年度数据丢掉
                continue

            try:
                val = float(item.get("val"))
            except Exception:
                continue

            # record start/end for this year if present
            periods[year] = (item.get("start"), item.get("end"))

            rows.append((year, val))
        if rows:
            s = (
                pd.DataFrame(rows, columns=["year", "value"])
                .drop_duplicates("year", keep="last")
                .set_index("year")["value"]
                .sort_index()
            )
            s.name = tag
            all_series.append(s)
            tag_names.append(tag)
            per_tag_periods[tag] = periods

    if not all_series:
        return pd.Series(dtype="float64"), None, {}

    # Merge all tags: for each year, use the value from the first (highest
    # priority) tag that has data.  This avoids gaps when a company switches
    # XBRL tags over time (e.g. SalesRevenueNet -> Revenues).
    # tags list order = priority order.
    merged_values = {}   # year -> value
    merged_tags = {}     # year -> tag name that provided the value
    merged_periods = {}  # year -> (start, end)

    # Build a lookup: tag_name -> series
    tag_to_series = {s.name: s for s in all_series}

    for tag in tags:  # iterate in priority order
        if tag not in tag_to_series:
            continue
        s = tag_to_series[tag]
        periods = per_tag_periods.get(tag, {})
        for year in s.index:
            if year not in merged_values:
                merged_values[year] = float(s.loc[year])
                merged_tags[year] = tag
                merged_periods[year] = periods.get(year, (None, None))

    combined = pd.Series(merged_values).sort_index()
    combined.name = merged_tags.get(max(merged_values.keys())) if merged_values else None
    return combined, merged_tags, merged_periods


# -----------------------------------------------
#  Quarterly variant of _series_from_tags
# -----------------------------------------------

def _quarterly_series_from_tags(
    facts: dict,
    tags: List[str],
    unit: str = "USD",
    is_instant: bool = False,
) -> Tuple[pd.Series, Optional[str], dict]:
    """Extract quarterly data from companyfacts.

    For *duration* items (income / cash-flow): looks for single-quarter
    values via the ``CYyyyyQq`` frame pattern (no "I" suffix).
    For *instant* items (balance sheet): looks for quarter-end snapshots
    via the ``CYyyyyQqI`` frame pattern.

    Falls back to start/end date analysis when frame is absent.

    Returns a Series indexed by ``"YYYYQn"`` strings, plus per-quarter
    tag dict and period dict – same shape as the annual helper.
    """
    import re
    from datetime import datetime as _dt

    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    ifrs_full = facts.get("facts", {}).get("ifrs-full", {})  # 20-F/40-F filers, see backfill_ifrs_full_usd
    yfinance_ns = facts.get("facts", {}).get("yfinance", {})  # synthetic fallback, see fetch_companyfacts
    all_series: list = []
    tag_names: list = []
    per_tag_periods: dict = {}

    for tag in tags:
        tag_info = us_gaap.get(tag) or ifrs_full.get(tag) or yfinance_ns.get(tag)
        if not tag_info:
            continue
        units_dict = tag_info.get("units", {})
        if unit not in units_dict:
            continue

        rows: list = []
        periods: dict = {}

        for item in units_dict[unit]:
            form = item.get("form", "")
            frame = item.get("frame") or ""
            start = item.get("start")
            end = item.get("end")

            if form not in ("10-Q", "10-K", "20-F", "40-F",
                            "10-Q/A", "10-K/A", "20-F/A", "40-F/A"):
                continue

            val = item.get("val")
            if val is None:
                continue
            try:
                val = float(val)
            except Exception:
                continue

            quarter_key = None

            if is_instant:
                # Balance-sheet snapshot at quarter end: CY2024Q1I
                m = re.fullmatch(r"CY(\d{4})Q([1-4])I", str(frame))
                if m:
                    quarter_key = f"{m.group(1)}Q{m.group(2)}"
            else:
                # Single-quarter flow: CY2024Q1
                m = re.fullmatch(r"CY(\d{4})Q([1-4])", str(frame))
                if m:
                    quarter_key = f"{m.group(1)}Q{m.group(2)}"
                elif start and end:
                    try:
                        s_dt = _dt.strptime(start, "%Y-%m-%d")
                        e_dt = _dt.strptime(end, "%Y-%m-%d")
                        days = (e_dt - s_dt).days + 1
                        if 80 <= days <= 100:
                            # Use midpoint to determine calendar quarter
                            # (avoids mis-labeling when fiscal quarter
                            #  end date spills into the next calendar quarter,
                            #  e.g. AAPL Apr 2 – Jul 1 → midpoint May → Q2)
                            from datetime import timedelta
                            mid = s_dt + timedelta(days=days // 2)
                            q = (mid.month - 1) // 3 + 1
                            quarter_key = f"{mid.year}Q{q}"
                    except Exception:
                        pass

            if quarter_key:
                rows.append((quarter_key, val))
                periods[quarter_key] = (start, end)

        if rows:
            s = (
                pd.DataFrame(rows, columns=["quarter", "value"])
                .drop_duplicates("quarter", keep="last")
                .set_index("quarter")["value"]
                .sort_index()
            )
            s.name = tag
            all_series.append(s)
            tag_names.append(tag)
            per_tag_periods[tag] = periods

    if not all_series:
        return pd.Series(dtype="float64"), None, {}

    # Priority-merge (same logic as annual)
    merged_values: dict = {}
    merged_tags: dict = {}
    merged_periods: dict = {}
    tag_to_series = {s.name: s for s in all_series}

    for tag in tags:
        if tag not in tag_to_series:
            continue
        s = tag_to_series[tag]
        tag_periods = per_tag_periods.get(tag, {})
        for qk in s.index:
            if qk not in merged_values:
                merged_values[qk] = float(s.loc[qk])
                merged_tags[qk] = tag
                merged_periods[qk] = tag_periods.get(qk, (None, None))

    combined = pd.Series(merged_values).sort_index()
    combined.name = (
        merged_tags.get(max(merged_values.keys())) if merged_values else None
    )
    return combined, merged_tags, merged_periods


def extract_quarterly_financials(facts: dict) -> dict:
    """Extract quarterly financial time series from companyfacts.

    Returns dict mapping metric name to ``(series, tag_dict, periods)``
    tuple.  Series are indexed by ``"YYYYQn"`` strings.
    """
    result: dict = {}

    # --- Duration / flow items ---
    for name, tags in [
        ("revenue", REVENUE_TAGS),
        ("net_income", NET_INCOME_TAGS),
        ("cfo", CFO_TAGS),
        ("capex", CAPEX_TAGS),
        ("gross_profit", GROSS_PROFIT_TAGS),
        ("operating_income", OPERATING_INCOME_TAGS),
        ("da", DA_TAGS),
        ("interest_expense", INTEREST_EXPENSE_TAGS),
        ("income_tax", INCOME_TAX_TAGS),
        ("income_before_tax", INCOME_BEFORE_TAX_TAGS),
        ("rd_expense", RD_EXPENSE_TAGS),
        ("sbc", SBC_TAGS),
        ("buyback", BUYBACK_TAGS),
        ("dividends_paid", DIVIDENDS_PAID_TAGS),
    ]:
        s, tag, periods = _quarterly_series_from_tags(facts, tags, unit="USD", is_instant=False)
        result[name] = (s, tag, periods)

    # --- Instant / balance-sheet items ---
    for name, tags in [
        ("cash", CASH_TAGS),
        ("marketable_securities_current", MARKETABLE_SECURITIES_CURRENT_TAGS),
        ("marketable_securities_noncurrent", MARKETABLE_SECURITIES_NONCURRENT_TAGS),
        ("long_term_debt", LONG_TERM_DEBT_TAGS),
        ("short_term_debt", SHORT_TERM_DEBT_TAGS),
        ("total_assets", TOTAL_ASSETS_TAGS),
        ("accounts_receivable", ACCOUNTS_RECEIVABLE_TAGS),
        ("stockholders_equity", STOCKHOLDERS_EQUITY_TAGS),
    ]:
        s, tag, periods = _quarterly_series_from_tags(facts, tags, unit="USD", is_instant=True)
        result[name] = (s, tag, periods)

    # --- Per-share (duration) ---
    for name, tags in [
        ("eps_diluted", EPS_DILUTED_TAGS),
        ("dividends_per_share", DIVIDENDS_PER_SHARE_TAGS),
    ]:
        s, tag, periods = _quarterly_series_from_tags(facts, tags, unit="USD/shares", is_instant=False)
        result[name] = (s, tag, periods)

    # --- Shares outstanding (try duration first, fallback to instant) ---
    s, tag, periods = _quarterly_series_from_tags(facts, SHARE_OUTSTANDING_TAGS, unit="shares", is_instant=False)
    if s.empty:
        s, tag, periods = _quarterly_series_from_tags(facts, SHARE_OUTSTANDING_TAGS, unit="shares", is_instant=True)
    result["shares_outstanding"] = (s, tag, periods)

    # --- Derived metrics ---
    cfo = result["cfo"][0]
    capex = result["capex"][0]
    idx = cfo.index.intersection(capex.index)
    fcf = (cfo.reindex(idx) - capex.reindex(idx)).rename("FreeCashFlow")
    result["fcf"] = (fcf, None, {})

    ltd = result["long_term_debt"][0]
    std = result["short_term_debt"][0]
    idx = ltd.index.union(std.index)
    total_debt = (ltd.reindex(idx).fillna(0) + std.reindex(idx).fillna(0)).rename("TotalDebt")
    result["total_debt"] = (total_debt, None, {})

    oi = result["operating_income"][0]
    da = result["da"][0]
    idx = oi.index.union(da.index)
    ebitda = (oi.reindex(idx).fillna(0) + da.reindex(idx).fillna(0)).rename("EBITDA")
    result["ebitda"] = (ebitda, None, {})

    c = result["cash"][0]
    ms_cur = result["marketable_securities_current"][0]
    ms_nc = result["marketable_securities_noncurrent"][0]
    idx = c.index.union(ms_cur.index).union(ms_nc.index)
    total_cash = (c.reindex(idx).fillna(0) + ms_cur.reindex(idx).fillna(0) + ms_nc.reindex(idx).fillna(0)).rename("TotalCash")
    result["total_cash"] = (total_cash, None, {})

    return result


# =======================================
#  Calculation layer: pure-ish functions
# =======================================

# 1. 计算净利率和 FCF/净利润比例
# 去掉 “净利润几乎为 0” 的年份（否则比值会爆炸）。
# 把 FCF/NI 限制在 [-150%, +150%] 之间，可以以后自己调这个区间。 《〈《〈《〈《〈
def compute_margins(
    revenue: pd.Series,
    net_income: pd.Series,
    fcf: pd.Series,
    avg_years: int = 5,
):
    # ---- 1. 净利率: Net Profit Margin = Net Income / Revenue ----
    common_ni = revenue.index.intersection(net_income.index)
    net_margin = (net_income[common_ni] / revenue[common_ni]).rename("NetMargin")

    # ---- 2. FCF to Profit margin = FCF / Net Income 比例，做一些清洗 + 限制 ----
    common_fcf = fcf.index.intersection(net_income.index)

    ni_for_ratio = net_income[common_fcf].copy()
    fcf_for_ratio = fcf[common_fcf].copy()

    # 2.1 去掉 “净利润绝对值太小” 的年份，防止 1 块钱净利润 / 巨大 FCF 变成几千倍
    # 规则：|Net Income| < 5% * |Revenue| 或者 太接近 0 的年份直接丢掉 
    rev_for_ratio = revenue.reindex(common_fcf)
    tiny_ni_mask = (ni_for_ratio.abs() < (0.05 * rev_for_ratio.abs())) | (ni_for_ratio.abs() < 1e-6)
    ni_for_ratio[tiny_ni_mask] = np.nan
    fcf_for_ratio[tiny_ni_mask] = np.nan

    # 2.2 原始比例
    raw_ratio = (fcf_for_ratio / ni_for_ratio)

    # 2.3 把极端值裁剪在一个合理区间内，比如 -1.5 ~ 1.5（即 -150% ~ 150%《〈《〈《〈《〈
    fcf_to_ni = raw_ratio.clip(lower=-1.5, upper=1.5).rename("FCF_to_NI")

    def last_n_avg(s: pd.Series, n: int) -> float:
        s = s.replace([np.inf, -np.inf], np.nan).dropna().tail(n)
        return float(s.mean()) if not s.empty else float("nan")

    avg_net_margin = last_n_avg(net_margin, avg_years)
    avg_fcf_to_ni = last_n_avg(fcf_to_ni, avg_years)

    return net_margin, fcf_to_ni, avg_net_margin, avg_fcf_to_ni

# 2. 计算 FCF Margin
#  优先级：
#    1）用最近 avg_years 年的 (FCF / Revenue) 清洗平均值
#    2）否则用 avg_net_margin * avg_fcf_to_
def compute_fcf_margin(
    revenue: pd.Series,
    fcf: pd.Series,
    avg_net_margin: float,
    avg_fcf_to_ni: float,
    avg_years: int = 5,
) -> float:
    """Compute the FCF margin to use for projections.

    Priority:
      1) Use recent cleaned average of (FCF / Revenue) (last `avg_years` years)
      2) Else use avg_net_margin * avg_fcf_to_ni
      3) Else fallback to 15%

    The returned margin is clipped to [-10%, 50%].
    """
    fr_all = (fcf / revenue).replace([np.inf, -np.inf], np.nan).dropna()
    fr_tail = fr_all.tail(avg_years)
    if not fr_tail.empty:
        # remove extreme values by clipping to 10%-90% quantiles then take mean
        low_q = fr_tail.quantile(0.10)
        high_q = fr_tail.quantile(0.90)
        fr_clean = fr_tail.clip(lower=low_q, upper=high_q)
        fcf_margin = float(fr_clean.mean())
    elif not (math.isnan(avg_net_margin) or math.isnan(avg_fcf_to_ni)):
        fcf_margin = avg_net_margin * avg_fcf_to_ni
    else:
        fcf_margin = 0.15

    # clamp to reasonable financial bounds
    fcf_margin = max(min(fcf_margin, 0.50), -0.10)
    return fcf_margin

# 3a. 获取 analyst consensus revenue estimates（绝对值）
def fetch_analyst_revenue(ticker: str) -> List[Tuple[int, float]]:
    """从 yfinance 获取 analyst consensus revenue estimates。

    返回 [(year_offset, revenue_value), ...] 按年排序。
    year_offset: 1 = 下一个财年, 2 = 再下一个。
    异常时返回空 list。
    """
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        rev_est = tk.revenue_estimate
        if rev_est is None or rev_est.empty:
            return []

        # yfinance reports analyst estimates in the filer's reporting
        # currency (e.g. TWD for TSM), not USD — convert so this is
        # comparable to the (already-USD) historical revenue series.
        currency = (tk.info or {}).get("financialCurrency") or "USD"
        fx = _yfinance_fx_to_usd(currency, [pd.Timestamp.now()])
        fx_rate = next(iter(fx.values()), 1.0)

        results = []
        for idx_label in rev_est.index:
            label = str(idx_label).strip().lower()
            if label in ("0y", "+1y"):
                offset = 1 if label == "0y" else 2
                if "avg" in rev_est.columns:
                    val = rev_est.loc[idx_label, "avg"]
                elif "mean" in rev_est.columns:
                    val = rev_est.loc[idx_label, "mean"]
                else:
                    continue
                if pd.notna(val) and float(val) > 0:
                    results.append((offset, float(val) * fx_rate))

        results.sort(key=lambda x: x[0])
        if results:
            pprint(f"Analyst revenue estimates: {[(o, f'${v/1e9:.1f}B') for o, v in results]}")
        return results
    except Exception as e:
        pprint(f"fetch_analyst_revenue failed: {e}")
        return []


# 3b. 采用分析师预测或历史 CAGR 作为收入增长率
def adopt_growth_rate(ticker: str, revenue: pd.Series) -> Tuple[float, str]:
    """返回 (growth_rate, source)。source 为 'analyst' / 'cagr' / 'default'。"""
    growth = None
    source = "default"

    # 1）尝试 yfinance 分析师预测
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        ef = t.earnings_forecasts
        if ef is not None and "revenueEstimate" in ef:
            g = ef["revenueEstimate"].get("growth")
            if isinstance(g, str) and g.endswith("%"):
                growth = float(g.strip("%")) / 100.0
            elif g is not None:
                growth = float(g)
            if growth is not None:
                source = "analyst"
    except Exception:
        growth = None

    # 2）fallback：最近 3–5 年 Revenue CAGR
    if growth is None:
        rev = revenue.dropna().tail(5)
        if len(rev) >= 2 and rev.iloc[0] > 0:
            n = len(rev) - 1
            growth = (rev.iloc[-1] / rev.iloc[0]) ** (1 / n) - 1.0
            source = "cagr"

    # 3）最后兜底一个保守值
    if growth is None:
        growth = 0.05
        source = "default"

    # 4）安全限制：防止极端值
    #    分析师预测：信任度高，只做宽松 clip（-20% ~ 50%）
    #    CAGR/默认值：保守 clip（-10% ~ 25%）
    if source == "analyst":
        growth = max(min(growth, 0.50), -0.20)
    else:
        growth = max(min(growth, 0.25), -0.10)

    return growth, source

# ============================
#  三阶段增长路径：10 年 + 永续 （根据行业估计10年的growth rate）
# ============================
def build_growth_curve(
    ticker: str,
    facts: dict,
    revenue: pd.Series,
    perpetual_growth: float,
    avg_years: int = 5,
    sector: Optional[str] = None,
) -> Tuple[list, float]:
    """
    返回：
        growth_path: 长度 10 的 list, 每一年对应的收入增长率
        terminal_g:  永续增长率（会结合 sector 范围修正）
    逻辑：
        1. 先用 adopt_growth_rate 得到一个"基准增长率" base_g
        2. 用 SIC → sector, 从 SECTOR_GROWTH 里取 fast/stable/terminal 区间
        3. 把 base_g 限制在 fast 区间，用作前 5 年目标
        4. 中间 5 年向 stable 区间收敛
        5. terminal_g 结合参数 perpetual_growth 和 sector 的 terminal 区间
    """

    # 1) 行业识别：优先用外部传入的 sector，否则自动推断
    if not sector:
        sector = infer_sector_from_facts(facts, ticker)
    ranges = SECTOR_GROWTH.get(sector, SECTOR_GROWTH["CONSUMER"])
    fast_low, fast_high = ranges["fast"]
    stable_low, stable_high = ranges["stable"]
    term_low, term_high = ranges["terminal"]

    # 2) 基准增长率（分析师预测 or 历史 CAGR）
    base_g, growth_source = adopt_growth_rate(ticker, revenue)
    pprint(f"Base growth rate adopted: {base_g:.2%} (source: {growth_source}) for sector {sector}")

    # fast 阶段目标增长率：
    #   - 有分析师预测时：直接用分析师增长率作为第一年，仅做宽松 clip
    #   - 无分析师预测时：clip 到行业 fast 区间
    if growth_source == "analyst":
        # 分析师预测代表近 1-2 年预期，直接作为 fast 阶段起点
        # 只用行业上限做安全 cap，不用下限抬高
        g_fast = min(base_g, max(fast_high, base_g * 0.8))  # 允许略超行业上限
        g_fast = max(g_fast, fast_low)  # 但不低于行业下限
        pprint(f"Fast stage growth rate set to: {g_fast:.2%} (analyst-driven, sector range {fast_low:.2%} - {fast_high:.2%})")
    else:
        g_fast = max(min(base_g, fast_high), fast_low)
        pprint(f"Fast stage growth rate set to: {g_fast:.2%} (range {fast_low:.2%} - {fast_high:.2%})")

    # stable 阶段目标增长率：
    #   - 高增长公司（fast > stable 上限的 2 倍）：用 stable 上限
    #   - 其他：用 stable 区间中值
    if g_fast > stable_high * 2:
        g_stable = stable_high
    else:
        g_stable = 0.5 * (stable_low + stable_high)
    pprint(f"Stable stage growth rate set to: {g_stable:.2%} (range {stable_low:.2%} - {stable_high:.2%})")


    # terminal growth：结合参数 perpetual_growth 和行业区间
    g_terminal_raw = perpetual_growth
    g_terminal = max(min(g_terminal_raw, term_high), term_low)

    # 3) 构造 10 年增长路径：
    #    Years 1–5: 从 g_fast 逐步过渡到 g_stable
    #    Years 6–10: 从 g_stable 逐步过渡到 g_terminal
    g1_5 = list(np.linspace(g_fast, g_stable, 5))
    g6_10 = list(np.linspace(g_stable, g_terminal, 5))

    growth_path = g1_5 + g6_10  # 共 10 个增长率
    pprint("Growth path:")
    pprint(growth_path)
    return growth_path, g_terminal

# ============================
#  两阶段增长路径：过去五年（avg. profit margin & avg. fcf profit margin ）来预测未来三年 
# ============================

# =======================================
#  主流程：构建 DCF 估值 （贴现现金流）
#  DCF（Discounted Cash Flow，贴现现金流）是一种估值方法：
# 把公司未来可获得的现金流折现到现在，把它们加总，得到公司当前的价值。
# 直观上就是“未来的钱值现在多少钱”。
# 折现未来若干年的自由现金流之和，再加上终值（terminal value）的现值：
#========================================
def build_dcf(
    ticker: str,
    required_return: float = 0.07,
    perpetual_growth: float = 0.025,
    avg_years: int = 5,
    projection_years: int = 10,   # 👈 默认做 10 年
    sector: Optional[str] = None, # 👈 新增：可手动指定 sector（TECH / CONSUMER / ...）
    save_raw_json: bool = False,
    save_raw_csv: bool = False,
):
    cik = fetch_cik(ticker)
    facts = fetch_companyfacts(cik, ticker=ticker)

    # Use extract_all_financials for full data access
    all_data = extract_all_financials(facts)

    revenue, revenue_tag, revenue_periods = all_data["revenue"]
    net_income, net_income_tag, net_income_periods = all_data["net_income"]
    cfo, cfo_tag, cfo_periods = all_data["cfo"]
    capex, capex_tag, capex_periods = all_data["capex"]
    fcf = all_data["fcf"][0]

    # Extra series + per-year tags for 10K output
    gross_profit, gross_profit_tag, _ = all_data["gross_profit"]
    eps_diluted, eps_tag, _ = all_data["eps_diluted"]
    cash, cash_tag, _ = all_data["cash"]
    total_cash = all_data["total_cash"][0]  # derived: cash + marketable securities
    ms_cur_tag = all_data["marketable_securities_current"][1]
    ms_nc_tag = all_data["marketable_securities_noncurrent"][1]
    total_debt = all_data["total_debt"][0]  # derived, no single tag
    long_term_debt_tag = all_data["long_term_debt"][1]
    short_term_debt_tag = all_data["short_term_debt"][1]
    shares_tag = all_data["shares_outstanding"][1]
    operating_income, oi_tag, _ = all_data["operating_income"]
    da, da_tag, _ = all_data["da"]
    ebitda = all_data["ebitda"][0]
    rd_expense, rd_tag, _ = all_data["rd_expense"]
    sbc, sbc_tag, _ = all_data["sbc"]
    buyback, buyback_tag, _ = all_data["buyback"]
    dividends_paid, divpaid_tag, _ = all_data["dividends_paid"]
    interest_expense, interest_tag, _ = all_data["interest_expense"]
    income_tax, tax_tag, _ = all_data["income_tax"]
    income_before_tax, ibt_tag, _ = all_data["income_before_tax"]
    stockholders_equity, equity_tag, _ = all_data["stockholders_equity"]
    total_assets, assets_tag, _ = all_data["total_assets"]
    accounts_receivable, ar_tag, _ = all_data["accounts_receivable"]
    dividends_per_share, dps_tag, _ = all_data["dividends_per_share"]

    # 得到net margin & FCF to margin 历史值
    net_margin, fcf_to_ni, avg_net_margin, avg_fcf_to_ni = compute_margins(
        revenue, net_income, fcf, avg_years
    )

    # 流通股数
    shares_series = all_data["shares_outstanding"][0]
    if shares_series is not None and not shares_series.empty:
        shares = float(shares_series.iloc[-1])
        # Sanity check: detect unit change (e.g. MCD switched from actual shares
        # to millions in XBRL without changing the unit tag).
        # If shares < 100,000 but revenue > $1B, likely reported in millions.
        latest_rev = float(revenue.iloc[-1]) if not revenue.empty else 0
        if shares < 100_000 and latest_rev > 1e9:
            # Cross-validate with yfinance
            try:
                import yfinance as yf
                yf_shares = yf.Ticker(ticker).info.get("sharesOutstanding")
                if yf_shares and yf_shares > 1e6:
                    print(f"  [Shares fix] SEC value {shares:.0f} looks like millions; "
                          f"using yfinance: {yf_shares/1e6:.1f}M")
                    shares = float(yf_shares)
            except Exception:
                # Fallback: if historical data had much larger values, multiply by 1e6
                if len(shares_series) >= 2:
                    prev = float(shares_series.iloc[-2])
                    if prev > 1e6 and shares < 1e5:
                        print(f"  [Shares fix] SEC unit change detected; "
                              f"multiplying {shares:.0f} by 1e6")
                        shares *= 1e6
    else:
        shares = None

    # Fallback: if SEC has no shares data at all, use yfinance
    if shares is None or (isinstance(shares, float) and (shares != shares)):  # NaN check
        try:
            import yfinance as yf
            yf_shares = yf.Ticker(ticker).info.get("sharesOutstanding")
            if yf_shares and yf_shares > 1e6:
                print(f"  [Shares fix] SEC has no shares data; "
                      f"using yfinance: {yf_shares/1e6:.1f}M")
                shares = float(yf_shares)
        except Exception:
            pass

    # ---------- 历史表 ----------
    years = sorted(set(revenue.index) | set(net_income.index) | set(fcf.index))
    years = [int(y) for y in years if isinstance(y, (int, np.integer))]

    def pick(s: pd.Series, y: int):
        try:
            return float(s.loc[y])
        except Exception:
            return np.nan

    def pick_raw(s: pd.Series, y: int):
        try:
            return float(s.loc[y])
        except Exception:
            return np.nan

    # Helper: pick tag name for a given year from per-year tag dict
    def pick_tag(tag_dict, y):
        if isinstance(tag_dict, dict):
            return tag_dict.get(y, "")
        return tag_dict or ""

    # Helper: pick period (start, end) for a given year
    def pick_period(periods: dict, y: int):
        p = periods.get(y)
        if not p:
            return (None, None)
        return p

    # Gross margin helper
    def gross_margin_pct(y):
        rev = pick(revenue, y)
        gp = pick(gross_profit, y)
        if np.isfinite(rev) and np.isfinite(gp) and rev != 0:
            return gp / rev * 100
        return np.nan

    # Ratio helpers
    def ratio_pct(numerator, denominator, y):
        n = pick(numerator, y)
        d = pick(denominator, y)
        if np.isfinite(n) and np.isfinite(d) and d != 0:
            return n / d * 100
        return np.nan

    def roic_pct(y):
        oi_val = pick(operating_income, y)
        tax_val = pick(income_tax, y)
        ibt_val = pick(income_before_tax, y)
        eq_val = pick(stockholders_equity, y)
        debt_val = pick(total_debt, y)
        cash_val = pick(total_cash, y)
        if not all(np.isfinite(v) for v in [oi_val, eq_val]) or eq_val == 0:
            return np.nan
        # Effective tax rate
        if np.isfinite(tax_val) and np.isfinite(ibt_val) and ibt_val != 0:
            eff_tax = tax_val / ibt_val
        else:
            eff_tax = 0.21  # fallback: US statutory rate
        nopat = oi_val * (1 - eff_tax)
        invested_capital = eq_val + (debt_val if np.isfinite(debt_val) else 0) - (cash_val if np.isfinite(cash_val) else 0)
        if invested_capital == 0:
            return np.nan
        return nopat / invested_capital * 100

    hist_df = pd.DataFrame({
        "Year": years,
        # --- Income Statement ---
        "Revenue (M)": [pick(revenue, y) / 1e6 for y in years],
        "Gross Margin (%)": [gross_margin_pct(y) for y in years],
        "Operating Income (M)": [pick(operating_income, y) / 1e6 for y in years],
        "Operating Margin (%)": [ratio_pct(operating_income, revenue, y) for y in years],
        "Net Income (M)": [pick(net_income, y) / 1e6 for y in years],
        "Net Profit Margin (%)": [pick_raw(net_margin, y) * 100 for y in years],
        "D&A (M)": [pick(da, y) / 1e6 for y in years],
        "EBITDA (M)": [pick(ebitda, y) / 1e6 for y in years],
        "R&D Expense (M)": [pick(rd_expense, y) / 1e6 for y in years],
        "SBC (M)": [pick(sbc, y) / 1e6 for y in years],
        "Interest Expense (M)": [pick(interest_expense, y) / 1e6 for y in years],
        "Income Tax (M)": [pick(income_tax, y) / 1e6 for y in years],
        "Effective Tax Rate (%)": [ratio_pct(income_tax, income_before_tax, y) for y in years],
        "EPS Diluted ($)": [pick(eps_diluted, y) for y in years],
        "Dividends Per Share ($)": [pick(dividends_per_share, y) for y in years],
        # --- Cash Flow ---
        "Cash from Operations (M)": [pick(cfo, y) / 1e6 for y in years],
        "CapEx (M)": [pick(capex, y) / 1e6 for y in years],
        "Free Cash Flow (M)": [pick(fcf, y) / 1e6 for y in years],
        "FCF to Profit Margin (%)": [pick_raw(fcf_to_ni, y) * 100 for y in years],
        "Buyback (M)": [pick(buyback, y) / 1e6 for y in years],
        "Dividends Paid (M)": [pick(dividends_paid, y) / 1e6 for y in years],
        # --- Balance Sheet ---
        "Cash (B)": [pick(cash, y) / 1e9 for y in years],
        "Total Cash (B)": [pick(total_cash, y) / 1e9 for y in years],
        "Total Debt (B)": [pick(total_debt, y) / 1e9 for y in years],
        "Total Assets (B)": [pick(total_assets, y) / 1e9 for y in years],
        "Accounts Receivable (B)": [pick(accounts_receivable, y) / 1e9 for y in years],
        "Stockholders Equity (B)": [pick(stockholders_equity, y) / 1e9 for y in years],
        "Shares Outstanding (M)": [pick(shares_series, y) / 1e6 for y in years],
        # --- Ratios (pure 10-K) ---
        "ROE (%)": [ratio_pct(net_income, stockholders_equity, y) for y in years],
        "ROA (%)": [ratio_pct(net_income, total_assets, y) for y in years],
        "ROIC (%)": [roic_pct(y) for y in years],
        # --- XBRL Tags ---
        "Revenue Tag": [pick_tag(revenue_tag, y) for y in years],
        "Net Income Tag": [pick_tag(net_income_tag, y) for y in years],
        "CFO Tag": [pick_tag(cfo_tag, y) for y in years],
        "CapEx Tag": [pick_tag(capex_tag, y) for y in years],
        "Gross Profit Tag": [pick_tag(gross_profit_tag, y) for y in years],
        "Operating Income Tag": [pick_tag(oi_tag, y) for y in years],
        "D&A Tag": [pick_tag(da_tag, y) for y in years],
        "R&D Tag": [pick_tag(rd_tag, y) for y in years],
        "SBC Tag": [pick_tag(sbc_tag, y) for y in years],
        "Interest Expense Tag": [pick_tag(interest_tag, y) for y in years],
        "Income Tax Tag": [pick_tag(tax_tag, y) for y in years],
        "EPS Tag": [pick_tag(eps_tag, y) for y in years],
        "DPS Tag": [pick_tag(dps_tag, y) for y in years],
        "Buyback Tag": [pick_tag(buyback_tag, y) for y in years],
        "Dividends Paid Tag": [pick_tag(divpaid_tag, y) for y in years],
        "Cash Tag": [pick_tag(cash_tag, y) for y in years],
        "MS Current Tag": [pick_tag(ms_cur_tag, y) for y in years],
        "MS Noncurrent Tag": [pick_tag(ms_nc_tag, y) for y in years],
        "LT Debt Tag": [pick_tag(long_term_debt_tag, y) for y in years],
        "ST Debt Tag": [pick_tag(short_term_debt_tag, y) for y in years],
        "Assets Tag": [pick_tag(assets_tag, y) for y in years],
        "AR Tag": [pick_tag(ar_tag, y) for y in years],
        "Equity Tag": [pick_tag(equity_tag, y) for y in years],
        "Shares Tag": [pick_tag(shares_tag, y) for y in years],
        "Start": [pick_period(revenue_periods, y)[0] for y in years],
        "End": [pick_period(revenue_periods, y)[1] for y in years],
    })

    # Transpose: years become columns (sorted ascending), metrics become rows
    hist_df = hist_df.set_index("Year").T
    hist_df = hist_df[sorted(hist_df.columns)]
    hist_df.index.name = None

    # ---------- 预测期（10 年） ----------
    if revenue.empty:
        raise ValueError("No revenue data available for this ticker")

    last_year = int(revenue.index.max())
    base_rev = float(revenue.loc[last_year])

    # 三阶段增长路径（10 年）+ 永续增速（用作 fallback）
    growth_path, terminal_g = build_growth_curve(
        ticker=ticker,
        facts=facts,
        revenue=revenue,
        perpetual_growth=perpetual_growth,
        avg_years=avg_years,
        sector=sector,
    )

    # 确保 projection_years 至少 10
    projection_years = max(projection_years, len(growth_path))

    # 尝试获取 analyst consensus revenue estimates
    analyst_rev = fetch_analyst_revenue(ticker)
    n_analyst = 0

    proj_years = [last_year + i for i in range(1, projection_years + 1)]
    proj_rev = []

    if analyst_rev:
        # --- Analyst-driven projection ---
        n_analyst = len(analyst_rev)
        pprint(f"Using {n_analyst} analyst revenue estimate(s) for years 1-{n_analyst}")

        # 前 N 年用 analyst 数据
        for _, rev_val in analyst_rev:
            proj_rev.append(rev_val)

        # 计算最后一个 analyst 年的隐含增长率
        if n_analyst >= 2:
            last_analyst_growth = (analyst_rev[-1][1] / analyst_rev[-2][1]) - 1.0
        else:
            last_analyst_growth = (analyst_rev[0][1] / base_rev) - 1.0

        # 剩余年份：从 last_analyst_growth 线性 taper 到 terminal_g
        remaining = projection_years - n_analyst
        if remaining > 0:
            taper = list(np.linspace(last_analyst_growth, terminal_g, remaining))
            cur_rev = proj_rev[-1]
            for g in taper:
                cur_rev *= (1.0 + g)
                proj_rev.append(cur_rev)

        # 重建 growth_path 用于输出
        growth_path = []
        growth_path.append((analyst_rev[0][1] / base_rev) - 1.0)  # year 1
        for i in range(1, n_analyst):
            growth_path.append((analyst_rev[i][1] / analyst_rev[i - 1][1]) - 1.0)
        if remaining > 0:
            growth_path.extend(taper)

        pprint(f"Analyst-driven growth path: {[f'{g:.1%}' for g in growth_path]}")
    else:
        # --- Fallback: 原始三阶段逻辑 ---
        cur_rev = base_rev
        for i in range(projection_years):
            g = growth_path[i] if i < len(growth_path) else growth_path[-1]
            cur_rev *= (1.0 + g)
            proj_rev.append(cur_rev)

    # 计算用于投影的 FCF margin（提取成独立函数以便测试/覆盖）
    fcf_margin = compute_fcf_margin(revenue, fcf, avg_net_margin, avg_fcf_to_ni, avg_years)
    proj_fcf = [r * fcf_margin for r in proj_rev]





    # 折现 + 终值（在第 projection_years 年之后）
    dfs = [(1.0 + required_return) ** i for i in range(1, projection_years + 1)]
    pv_fcfs = [proj_fcf[i] / dfs[i] for i in range(projection_years)]

    terminal_fcf = proj_fcf[-1] * (1.0 + terminal_g)
    terminal_value = terminal_fcf / (required_return - terminal_g)
    pv_terminal = terminal_value / ((1.0 + required_return) ** projection_years)

    todays_value = sum(pv_fcfs) + pv_terminal
    fair_value = todays_value / shares if (shares is not None and shares > 0) else np.nan

    # ---------- 预测表 ----------
    # For projections, use the tag from the most recent year as the reference
    latest_rev_tag = pick_tag(revenue_tag, last_year)
    latest_cfo_tag = pick_tag(cfo_tag, last_year)
    latest_capex_tag = pick_tag(capex_tag, last_year)
    base_period = revenue_periods.get(last_year, (None, None))

    # 标记每年的预测来源
    estimate_source = []
    for i in range(projection_years):
        if i < n_analyst:
            estimate_source.append("Analyst")
        else:
            estimate_source.append("Model")

    proj_df = pd.DataFrame({
        "Year": proj_years,
        "Revenue (M)": [x / 1e6 for x in proj_rev],
        "Free Cash Flow (M)": [x / 1e6 for x in proj_fcf],
        "Growth Rate (%)": [g * 100 for g in growth_path[:projection_years]],
        "Estimate Source": estimate_source,
        "Discount Factor": [round(x, 2) for x in dfs],
        "PV of Future Cash Flow (M)": [x / 1e6 for x in pv_fcfs],
        "Revenue Tag": [latest_rev_tag] * len(proj_years),
        "CFO Tag": [latest_cfo_tag] * len(proj_years),
        "CapEx Tag": [latest_capex_tag] * len(proj_years),
        "Base Start": [base_period[0]] * len(proj_years),
        "Base End": [base_period[1]] * len(proj_years),
    })

    # ---------- 汇总元数据 ----------
    units = facts.get("facts", {}).get("us-gaap", {}).get("Revenues", {}).get("units", {})
    currency = next(iter(units.keys()), "N/A")

    # prefer caller-provided sector (manual override) else infer from facts
    meta = {
        "Avg Net Profit Margin (%)": avg_net_margin * 100 if not math.isnan(avg_net_margin) else np.nan,
        "Avg FCF/Net Income (%)": avg_fcf_to_ni * 100 if not math.isnan(avg_fcf_to_ni) else np.nan,
        "Sector": sector if sector is not None else infer_sector_from_facts(facts, ticker),
        "Growth Model": "Analyst + Taper 10-Year" if n_analyst > 0 else "3-Stage 10-Year",
        "Analyst Years": n_analyst,
        # tags used to produce the core series
        "Revenue Tag": latest_rev_tag,
        "Net Income Tag": pick_tag(net_income_tag, last_year),
        "CFO Tag": latest_cfo_tag,
        "CapEx Tag": latest_capex_tag,
        "Adopted Growth Rate Year1 (%)": growth_path[0] * 100,
        "Adopted Growth Rate Year10 (%)": growth_path[-1] * 100,
        "Terminal Growth (%)": terminal_g * 100,
        "Required Return (%)": required_return * 100,
        "PV Terminal (M)": pv_terminal / 1e6,
        "Enterprise Value (M)": todays_value / 1e6,
        "Shares Outstanding (M)": shares / 1e6 if shares else np.nan,
        "Fair Value / Share ": fair_value,
        "Currency": currency,
    }

    # Transpose: years become columns, metrics become rows
    proj_df = proj_df.set_index("Year").T
    proj_df.index.name = None

    return hist_df, proj_df, meta

# =======================================
#  Market data builder (stock market data, separate from 10-K)
# =======================================

BASIC_SHARES_TAGS = [
    "CommonStockSharesOutstanding",
    "EntityCommonStockSharesOutstanding",
]


def build_market_data(ticker: str, all_data: dict, years: list,
                      facts: dict = None) -> pd.DataFrame:
    """Build historical + current market data table.

    Uses yfinance for year-end prices and current market snapshot.
    Combines with 10-K financial data to compute valuation ratios per year.

    Market Cap uses **basic shares outstanding** (CommonStockSharesOutstanding)
    per industry convention.  P/E uses diluted EPS (already per-share).
    """
    import yfinance as yf
    from dcf_utils import get_historical_year_end_prices

    years = sorted(years)
    start_year = years[0]
    end_year = years[-1]

    # --- Historical year-end prices ---
    prices = get_historical_year_end_prices(ticker, start_year, end_year)

    # --- Current market data from yfinance ---
    tk = yf.Ticker(ticker)
    try:
        info = tk.info
    except Exception:
        info = {}

    # --- Basic shares outstanding for Market Cap (not diluted) ---
    if facts is not None:
        basic_shares, _, _ = _series_from_tags(facts, BASIC_SHARES_TAGS, unit="shares")
    else:
        basic_shares = pd.Series(dtype="float64")
    # Fallback: if no basic shares data, use whatever is in all_data
    if basic_shares.empty:
        basic_shares = all_data["shares_outstanding"][0]

    # --- Extract financial series for ratio computation ---
    revenue = all_data["revenue"][0]
    net_income = all_data["net_income"][0]
    eps_diluted = all_data["eps_diluted"][0]
    total_debt = all_data["total_debt"][0]
    total_cash = all_data["total_cash"][0]
    stockholders_equity = all_data["stockholders_equity"][0]
    ebitda = all_data["ebitda"][0]
    dividends_per_share = all_data["dividends_per_share"][0]
    sbc = all_data["sbc"][0]
    fcf = all_data["fcf"][0]
    buyback = all_data["buyback"][0]
    dividends_paid = all_data["dividends_paid"][0]

    def _pick(s, y):
        try:
            return float(s.loc[y])
        except Exception:
            return np.nan

    # --- Build historical rows ---
    hist_rows = {}
    for y in years:
        price = _pick(prices, y) if y in prices.index else np.nan
        rev = _pick(revenue, y)
        ni = _pick(net_income, y)
        eps = _pick(eps_diluted, y)
        sh = _pick(basic_shares, y)  # Basic shares for Market Cap
        debt = _pick(total_debt, y)
        cash = _pick(total_cash, y)
        equity = _pick(stockholders_equity, y)
        ebitda_val = _pick(ebitda, y)
        dps = _pick(dividends_per_share, y)
        sbc_val = _pick(sbc, y)
        fcf_val = _pick(fcf, y)
        buyback_val = _pick(buyback, y)
        divpaid_val = _pick(dividends_paid, y)

        # Market Cap = Price * Basic Shares Outstanding
        mkt_cap = price * sh if np.isfinite(price) and np.isfinite(sh) else np.nan
        # Enterprise Value = Market Cap + Debt - Cash
        ev = np.nan
        if np.isfinite(mkt_cap):
            ev = mkt_cap + (debt if np.isfinite(debt) else 0) - (cash if np.isfinite(cash) else 0)

        hist_rows[y] = {
            "Year-End Price ($)": round(price, 2) if np.isfinite(price) else np.nan,
            "Market Cap (B)": mkt_cap / 1e9 if np.isfinite(mkt_cap) else np.nan,
            "Enterprise Value (B)": ev / 1e9 if np.isfinite(ev) else np.nan,
            "Trailing P/E": price / eps if np.isfinite(price) and np.isfinite(eps) and eps > 0 else np.nan,
            "P/S": mkt_cap / rev if np.isfinite(mkt_cap) and np.isfinite(rev) and rev > 0 else np.nan,
            "P/B": mkt_cap / equity if np.isfinite(mkt_cap) and np.isfinite(equity) and equity > 0 else np.nan,
            "EV/Revenue": ev / rev if np.isfinite(ev) and np.isfinite(rev) and rev > 0 else np.nan,
            "EV/EBITDA": ev / ebitda_val if np.isfinite(ev) and np.isfinite(ebitda_val) and ebitda_val > 0 else np.nan,
            "Dividend Yield (%)": dps / price * 100 if np.isfinite(dps) and np.isfinite(price) and price > 0 else np.nan,
            "SBC/Revenue (%)": sbc_val / rev * 100 if np.isfinite(sbc_val) and np.isfinite(rev) and rev > 0 else np.nan,
            "FCF Yield (%)": fcf_val / mkt_cap * 100 if np.isfinite(fcf_val) and np.isfinite(mkt_cap) and mkt_cap > 0 else np.nan,
            "Adjusted FCF Yield (%)": (fcf_val - sbc_val) / mkt_cap * 100 if np.isfinite(fcf_val) and np.isfinite(sbc_val) and np.isfinite(mkt_cap) and mkt_cap > 0 else np.nan,
            "Shareholder Yield (%)": (buyback_val + divpaid_val) / mkt_cap * 100 if np.isfinite(buyback_val) and np.isfinite(divpaid_val) and np.isfinite(mkt_cap) and mkt_cap > 0 else np.nan,
        }

    # Build DataFrame: metrics as rows, years as columns
    market_df = pd.DataFrame(hist_rows)
    market_df.index.name = None

    # --- Add "Current" column from yfinance .info ---
    current = {
        "Year-End Price ($)": info.get("currentPrice"),
        "Market Cap (B)": info.get("marketCap", 0) / 1e9 if info.get("marketCap") else np.nan,
        "Enterprise Value (B)": info.get("enterpriseValue", 0) / 1e9 if info.get("enterpriseValue") else np.nan,
        "Trailing P/E": info.get("trailingPE"),
        "P/S": info.get("priceToSalesTrailing12Months"),
        "P/B": info.get("priceToBook"),
        "EV/Revenue": info.get("enterpriseToRevenue"),
        "EV/EBITDA": info.get("enterpriseToEbitda"),
        "Dividend Yield (%)": info.get("dividendYield", 0) * 100 if info.get("dividendYield") else np.nan,
    }
    market_df["Current"] = pd.Series(current)

    # --- Add extra current-only rows ---
    extra_current = {
        "Forward P/E": info.get("forwardPE"),
        "PEG Ratio": info.get("pegRatio"),
        "Beta": info.get("beta"),
        "52-Week High ($)": info.get("fiftyTwoWeekHigh"),
        "52-Week Low ($)": info.get("fiftyTwoWeekLow"),
        "Forward EPS ($)": info.get("forwardEps"),
        "Trailing EPS ($)": info.get("trailingEps"),
        "Revenue Growth (%)": info.get("revenueGrowth", 0) * 100 if info.get("revenueGrowth") else np.nan,
        "Earnings Growth (%)": info.get("earningsGrowth", 0) * 100 if info.get("earningsGrowth") else np.nan,
        "Analyst Target Mean ($)": info.get("targetMeanPrice"),
        "Analyst Target High ($)": info.get("targetHighPrice"),
        "Analyst Target Low ($)": info.get("targetLowPrice"),
        "Analyst Recommendation": info.get("recommendationKey"),
        "Number of Analysts": info.get("numberOfAnalystOpinions"),
        "Short Ratio": info.get("shortRatio"),
        "Debt/Equity": info.get("debtToEquity"),
    }
    for k, v in extra_current.items():
        row = {y: np.nan for y in years}
        row["Current"] = v
        market_df.loc[k] = pd.Series(row)

    return market_df


# =======================================
#  Quarterly 10-Q table builder
# =======================================

def _fy_to_cal_quarters(fy_year: int, fy_end_month: int) -> list:
    """Map a fiscal year to its 4 calendar-quarter keys (FQ1→FQ4)."""
    result = []
    for fq in range(1, 5):
        months_before = 3 * (4 - fq)
        end_month = fy_end_month - months_before
        end_year = fy_year
        while end_month <= 0:
            end_month += 12
            end_year -= 1
        cal_q = (end_month - 1) // 3 + 1
        result.append(f"{end_year}Q{cal_q}")
    return result


def _fill_fiscal_q4(qdata: dict, adata: dict, fy_end_month: int) -> set:
    """Derive missing fiscal-Q4 quarters: Q4 = Annual − Q1 − Q2 − Q3.

    Modifies *qdata* series in-place and returns the set of calendar-quarter
    keys that were calculated (so they can be marked in the output).
    """
    calculated: set = set()

    # --- Additive flow metrics ---
    additive = [
        "revenue", "net_income", "cfo", "capex", "gross_profit",
        "operating_income", "da", "interest_expense", "income_tax",
        "income_before_tax", "rd_expense", "sbc", "buyback", "dividends_paid",
    ]
    pershare = ["eps_diluted", "dividends_per_share"]

    for fy_year in sorted(adata["revenue"][0].index):
        fy_year = int(fy_year)
        cqs = _fy_to_cal_quarters(fy_year, fy_end_month)
        q4_key = cqs[3]
        other_keys = cqs[:3]

        for metric in additive + pershare:
            q_s = qdata[metric][0]
            a_s = adata[metric][0]
            if q4_key in q_s.index or fy_year not in a_s.index:
                continue
            if not all(k in q_s.index for k in other_keys):
                continue
            q_s.loc[q4_key] = float(a_s.loc[fy_year]) - sum(
                float(q_s.loc[k]) for k in other_keys
            )
            calculated.add(q4_key)

        # Shares (weighted average): Q4 ≈ 4×Annual − Q1 − Q2 − Q3
        q_s = qdata["shares_outstanding"][0]
        a_s = adata["shares_outstanding"][0]
        if q4_key not in q_s.index and fy_year in a_s.index:
            if all(k in q_s.index for k in other_keys):
                annual = float(a_s.loc[fy_year])
                others = sum(float(q_s.loc[k]) for k in other_keys)
                q_s.loc[q4_key] = 4 * annual - others
                calculated.add(q4_key)

        # Balance-sheet items: fiscal year-end snapshot = fiscal Q4 snapshot
        for metric in [
            "cash", "marketable_securities_current",
            "marketable_securities_noncurrent",
            "long_term_debt", "short_term_debt",
            "total_assets", "accounts_receivable", "stockholders_equity",
        ]:
            q_s = qdata[metric][0]
            a_s = adata[metric][0]
            if q4_key not in q_s.index and fy_year in a_s.index:
                q_s.loc[q4_key] = float(a_s.loc[fy_year])
                calculated.add(q4_key)

    # --- Recompute derived metrics for new quarters ---
    for q in calculated:
        cfo_s, capex_s = qdata["cfo"][0], qdata["capex"][0]
        if q in cfo_s.index and q in capex_s.index:
            qdata["fcf"][0].loc[q] = cfo_s.loc[q] - capex_s.loc[q]

        oi_s, da_s = qdata["operating_income"][0], qdata["da"][0]
        oi_v = oi_s.loc[q] if q in oi_s.index else 0
        da_v = da_s.loc[q] if q in da_s.index else 0
        if q in oi_s.index or q in da_s.index:
            qdata["ebitda"][0].loc[q] = oi_v + da_v

        for srcs, dest in [
            (["long_term_debt", "short_term_debt"], "total_debt"),
            (["cash", "marketable_securities_current",
              "marketable_securities_noncurrent"], "total_cash"),
        ]:
            vals = [
                qdata[s][0].loc[q] if q in qdata[s][0].index else 0
                for s in srcs
            ]
            qdata[dest][0].loc[q] = sum(vals)

    return calculated


def build_quarterly_table(ticker: str, num_quarters: int = 10) -> pd.DataFrame:
    """Build a quarterly financial table (last *num_quarters* quarters).

    Structure mirrors the annual 10-K table: metrics as rows, quarters as
    columns.  Data comes from SEC 10-Q filings, with missing fiscal-Q4
    derived from 10-K annual data (marked as "Calculated" in the output).
    """
    from datetime import datetime as _dt

    cik = fetch_cik(ticker)
    facts = fetch_companyfacts(cik, ticker=ticker)
    qdata = extract_quarterly_financials(facts)
    adata = extract_all_financials(facts)

    # --- Determine fiscal year-end month ---
    fy_end_month = 12
    for year in sorted(adata["revenue"][2].keys(), reverse=True):
        _, end = adata["revenue"][2][year]
        if end:
            fy_end_month = _dt.strptime(end, "%Y-%m-%d").month
            break

    # --- Fill missing fiscal Q4 ---
    calculated_quarters = _fill_fiscal_q4(qdata, adata, fy_end_month)

    # Collect all quarter keys and take last N
    all_quarters: set = set()
    for _name, (s, _, _) in qdata.items():
        if not s.empty:
            all_quarters.update(s.index.tolist())
    quarters = sorted(all_quarters)[-num_quarters:]

    # --- helpers ---
    def pick(s: pd.Series, q: str):
        try:
            return float(s.loc[q])
        except Exception:
            return np.nan

    def pick_tag(tag_dict, q):
        if isinstance(tag_dict, dict):
            return tag_dict.get(q, "")
        return tag_dict or ""

    def pick_period(periods: dict, q: str):
        p = periods.get(q) if isinstance(periods, dict) else None
        return p if p else (None, None)

    # --- unpack series ---
    revenue        = qdata["revenue"][0]
    gross_profit   = qdata["gross_profit"][0]
    operating_income = qdata["operating_income"][0]
    net_income     = qdata["net_income"][0]
    da             = qdata["da"][0]
    ebitda         = qdata["ebitda"][0]
    rd_expense     = qdata["rd_expense"][0]
    sbc            = qdata["sbc"][0]
    buyback        = qdata["buyback"][0]
    dividends_paid_q = qdata["dividends_paid"][0]
    interest_expense = qdata["interest_expense"][0]
    income_tax     = qdata["income_tax"][0]
    income_before_tax = qdata["income_before_tax"][0]
    eps_diluted    = qdata["eps_diluted"][0]
    dividends_per_share = qdata["dividends_per_share"][0]
    cfo            = qdata["cfo"][0]
    capex          = qdata["capex"][0]
    fcf            = qdata["fcf"][0]
    cash           = qdata["cash"][0]
    total_cash     = qdata["total_cash"][0]
    total_debt     = qdata["total_debt"][0]
    total_assets   = qdata["total_assets"][0]
    accounts_receivable_q = qdata["accounts_receivable"][0]
    stockholders_equity = qdata["stockholders_equity"][0]
    shares         = qdata["shares_outstanding"][0]

    # --- ratio helpers ---
    def ratio_pct(num, den, q):
        n, d = pick(num, q), pick(den, q)
        if np.isfinite(n) and np.isfinite(d) and d != 0:
            return n / d * 100
        return np.nan

    # --- tags ---
    rev_tag      = qdata["revenue"][1]
    ni_tag       = qdata["net_income"][1]
    cfo_tag_d    = qdata["cfo"][1]
    capex_tag_d  = qdata["capex"][1]
    gp_tag       = qdata["gross_profit"][1]
    oi_tag       = qdata["operating_income"][1]
    da_tag_d     = qdata["da"][1]
    rd_tag_d     = qdata["rd_expense"][1]
    sbc_tag_d    = qdata["sbc"][1]
    buyback_tag_d = qdata["buyback"][1]
    divpaid_tag_d = qdata["dividends_paid"][1]
    int_tag      = qdata["interest_expense"][1]
    tax_tag_d    = qdata["income_tax"][1]
    eps_tag_d    = qdata["eps_diluted"][1]
    dps_tag_d    = qdata["dividends_per_share"][1]
    cash_tag_d   = qdata["cash"][1]
    ms_cur_tag   = qdata["marketable_securities_current"][1]
    ms_nc_tag    = qdata["marketable_securities_noncurrent"][1]
    lt_debt_tag  = qdata["long_term_debt"][1]
    st_debt_tag  = qdata["short_term_debt"][1]
    assets_tag_d = qdata["total_assets"][1]
    ar_tag_d     = qdata["accounts_receivable"][1]
    equity_tag_d = qdata["stockholders_equity"][1]
    shares_tag_d = qdata["shares_outstanding"][1]
    rev_periods  = qdata["revenue"][2]

    # --- build DataFrame ---
    df = pd.DataFrame({
        "Quarter": quarters,
        # Data source marker
        "Data Source": [
            "10-K Derived" if q in calculated_quarters else "10-Q"
            for q in quarters
        ],
        # Income Statement
        "Revenue (M)":           [pick(revenue, q) / 1e6 for q in quarters],
        "Gross Margin (%)":      [ratio_pct(gross_profit, revenue, q) for q in quarters],
        "Operating Income (M)":  [pick(operating_income, q) / 1e6 for q in quarters],
        "Operating Margin (%)":  [ratio_pct(operating_income, revenue, q) for q in quarters],
        "Net Income (M)":        [pick(net_income, q) / 1e6 for q in quarters],
        "Net Profit Margin (%)": [ratio_pct(net_income, revenue, q) for q in quarters],
        "D&A (M)":               [pick(da, q) / 1e6 for q in quarters],
        "EBITDA (M)":            [pick(ebitda, q) / 1e6 for q in quarters],
        "R&D Expense (M)":       [pick(rd_expense, q) / 1e6 for q in quarters],
        "SBC (M)":               [pick(sbc, q) / 1e6 for q in quarters],
        "Interest Expense (M)":  [pick(interest_expense, q) / 1e6 for q in quarters],
        "Income Tax (M)":        [pick(income_tax, q) / 1e6 for q in quarters],
        "Effective Tax Rate (%)": [ratio_pct(income_tax, income_before_tax, q) for q in quarters],
        "EPS Diluted ($)":       [pick(eps_diluted, q) for q in quarters],
        "Dividends Per Share ($)": [pick(dividends_per_share, q) for q in quarters],
        # Cash Flow
        "Cash from Operations (M)": [pick(cfo, q) / 1e6 for q in quarters],
        "CapEx (M)":                [pick(capex, q) / 1e6 for q in quarters],
        "Free Cash Flow (M)":      [pick(fcf, q) / 1e6 for q in quarters],
        "FCF to Profit Margin (%)": [
            (pick(fcf, q) / pick(net_income, q) * 100)
            if np.isfinite(pick(net_income, q)) and abs(pick(net_income, q)) > 1e-6
            else np.nan
            for q in quarters
        ],
        "Buyback (M)":              [pick(buyback, q) / 1e6 for q in quarters],
        "Dividends Paid (M)":       [pick(dividends_paid_q, q) / 1e6 for q in quarters],
        # Balance Sheet
        "Cash (B)":                 [pick(cash, q) / 1e9 for q in quarters],
        "Total Cash (B)":          [pick(total_cash, q) / 1e9 for q in quarters],
        "Total Debt (B)":          [pick(total_debt, q) / 1e9 for q in quarters],
        "Total Assets (B)":        [pick(total_assets, q) / 1e9 for q in quarters],
        "Accounts Receivable (B)": [pick(accounts_receivable_q, q) / 1e9 for q in quarters],
        "Stockholders Equity (B)": [pick(stockholders_equity, q) / 1e9 for q in quarters],
        "Shares Outstanding (M)":  [pick(shares, q) / 1e6 for q in quarters],
        # Tags
        "Revenue Tag":           [pick_tag(rev_tag, q) for q in quarters],
        "Net Income Tag":        [pick_tag(ni_tag, q) for q in quarters],
        "CFO Tag":               [pick_tag(cfo_tag_d, q) for q in quarters],
        "CapEx Tag":             [pick_tag(capex_tag_d, q) for q in quarters],
        "Gross Profit Tag":      [pick_tag(gp_tag, q) for q in quarters],
        "Operating Income Tag":  [pick_tag(oi_tag, q) for q in quarters],
        "D&A Tag":               [pick_tag(da_tag_d, q) for q in quarters],
        "R&D Tag":               [pick_tag(rd_tag_d, q) for q in quarters],
        "SBC Tag":               [pick_tag(sbc_tag_d, q) for q in quarters],
        "Interest Expense Tag":  [pick_tag(int_tag, q) for q in quarters],
        "Income Tax Tag":        [pick_tag(tax_tag_d, q) for q in quarters],
        "EPS Tag":               [pick_tag(eps_tag_d, q) for q in quarters],
        "DPS Tag":               [pick_tag(dps_tag_d, q) for q in quarters],
        "Buyback Tag":           [pick_tag(buyback_tag_d, q) for q in quarters],
        "Dividends Paid Tag":    [pick_tag(divpaid_tag_d, q) for q in quarters],
        "Cash Tag":              [pick_tag(cash_tag_d, q) for q in quarters],
        "MS Current Tag":        [pick_tag(ms_cur_tag, q) for q in quarters],
        "MS Noncurrent Tag":     [pick_tag(ms_nc_tag, q) for q in quarters],
        "LT Debt Tag":           [pick_tag(lt_debt_tag, q) for q in quarters],
        "ST Debt Tag":           [pick_tag(st_debt_tag, q) for q in quarters],
        "Assets Tag":            [pick_tag(assets_tag_d, q) for q in quarters],
        "AR Tag":                [pick_tag(ar_tag_d, q) for q in quarters],
        "Equity Tag":            [pick_tag(equity_tag_d, q) for q in quarters],
        "Shares Tag":            [pick_tag(shares_tag_d, q) for q in quarters],
        "Start":                 [pick_period(rev_periods, q)[0] for q in quarters],
        "End":                   [pick_period(rev_periods, q)[1] for q in quarters],
    })

    df = df.set_index("Quarter").T
    df.index.name = None
    return df


#python dcf_builder.py --ticker GOOGL --required 0.07 --perp 0.025 --avg-years 5
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--required", type=float, default=0.07)
    ap.add_argument("--perp", type=float, default=0.025)
    ap.add_argument("--years", type=int, default=4)
    ap.add_argument("--avg-years", type=int, default=5)
    ap.add_argument("--no-raw-json", action="store_true", help="不保存原始 JSON")
    ap.add_argument("--no-raw-csv", action="store_true", help="不保存原始 CSV")
    ap.add_argument("--sector", type=str, default=None,
                help="手动指定行业 (TECH / CONSUMER / BANK / INSURANCE / ENERGY / PHARMA)。若不指定则自动根据 SIC 判定")
    args = ap.parse_args()

    ticker_upper = args.ticker.upper()
    currYear = pd.Timestamp.now().year
    import os
    out_dir = os.path.join("output", "stocks", ticker_upper)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{currYear}_dcf_{ticker_upper}")
    # --- Sector inference ---
    if args.sector:  
        # 用户手动指定
        sector = args.sector.upper()
        sector_source = "manual"
    else:
        # 自动从 SEC facts 推断
        cik = fetch_cik(args.ticker)
        facts = fetch_companyfacts(cik, ticker=args.ticker)
        sector = infer_sector_from_facts(facts, args.ticker)
        sector_source = "sic"

    print(f"[INFO] Sector = {sector} (source: {sector_source})")

    # --- main DCF flow ---
    hist, proj, meta = build_dcf(
        ticker=args.ticker,
        required_return=args.required,
        perpetual_growth=args.perp,
        avg_years=args.avg_years,
        projection_years=args.years,
        sector=sector,
    )

    # Save DCF outputs
    hist.to_csv(base + ".csv", index=True)
    proj.to_csv(base + "_proj.csv", index=True)
    with open(base + "_meta.json", "w") as f:
        json.dump({
            k: (f"{v:,.2f}" if isinstance(v, (int, float)) else v)
            for k, v in meta.items()
        }, f, indent=2)

    # --- Build & save market data (separate from 10-K) ---
    cik = fetch_cik(args.ticker)
    facts = fetch_companyfacts(cik, ticker=args.ticker)
    all_data = extract_all_financials(facts)
    years = [int(c) for c in hist.columns if str(c).isdigit()]
    market_df = build_market_data(args.ticker, all_data, years, facts=facts)
    market_base = os.path.join(out_dir, f"{currYear}_market_{ticker_upper}")
    market_df.to_csv(market_base + ".csv", index=True)
    print(f"[Saved] {market_base}.csv")

    # --- Build & save quarterly table (last 10 quarters) ---
    quarterly_df = build_quarterly_table(args.ticker, num_quarters=10)
    quarterly_path = os.path.join(out_dir, f"{currYear}_quarterly_{ticker_upper}.csv")
    quarterly_df.to_csv(quarterly_path, index=True)
    print(f"[Saved] {quarterly_path}")

    # --- Save raw companyfacts JSON ---
    if not args.no_raw_json:

        fname_json = base + "_raw.json"
        with open(fname_json, "w") as f:
            json.dump(facts, f, indent=2)
        # print(f"[Saved] {fname_json}")

    # --- Save raw CSV ---
    if not args.no_raw_csv:
        cik = fetch_cik(args.ticker)
        facts = fetch_companyfacts(cik, ticker=args.ticker)

        raw_df = companyfacts_to_table(facts)
        fname_csv = base + "_raw.csv"
        raw_df.to_csv(fname_csv, index=False)
        print(f"[Saved] {fname_csv}")

    print(f"\nAll done. Files saved with base name: {base}\n")

# ============================
#  Test / command line entry point
# ============================

# Entry 1: Uncomment below to enable command line execution
# if __name__ == "__main__":
#     main()

# ============================
#  Test / direct function call entry point
# ============================

# Entry 2: 直接在代码里调用，不用命令行参数
import json
import pandas as pd
#   直接调用的入口函数
def run_dcf_once(
    ticker: str = "GOOGL",
    required_return: float = 0.07,
    perpetual_growth: float = 0.025,
    projection_years: int = 4,
    avg_years: int = 5,
    save_raw_json: bool = False,
    save_raw_csv: bool = False,
    sector: Optional[str] = None,
    sector_source: Optional[str] = None,
):
    """
    直接在代码里调用的入口，不再用命令行参数。
    """
    ticker_upper = ticker.upper()
    curr_year = pd.Timestamp.now().year
    import os
    out_dir = os.path.join("output", "stocks", ticker_upper)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{curr_year}_dcf_{ticker_upper}")
    
    # --- main DCF flow ---
    hist, proj, meta = build_dcf(
        ticker=ticker,
        required_return=required_return,
        perpetual_growth=perpetual_growth,
        avg_years=avg_years,
        projection_years=projection_years,
        sector=sector,
    )

    # 保存 DCF 结果
    hist.to_csv(base + "_10K.csv", index=True)
    proj.to_csv(base + "_proj.csv", index=True)
    with open(base + "_meta.json", "w") as f:
        json.dump(
            {
                k: (f"{v:,.2f}" if isinstance(v, (int, float)) else v)
                for k, v in meta.items()
            },
            f,
            indent=2,
        )

    # --- Build & save market data ---
    cik = fetch_cik(ticker)
    facts = fetch_companyfacts(cik, ticker=ticker)
    all_data = extract_all_financials(facts)
    years = [int(c) for c in hist.columns if str(c).isdigit()]
    market_df = build_market_data(ticker, all_data, years, facts=facts)
    market_base = os.path.join(out_dir, f"{curr_year}_market_{ticker_upper}")
    market_df.to_csv(market_base + ".csv", index=True)
    print(f"[Saved] {market_base}.csv")

    # --- Build & save quarterly table ---
    quarterly_df = build_quarterly_table(ticker, num_quarters=10)
    quarterly_path = os.path.join(out_dir, f"{curr_year}_quarterly_{ticker_upper}.csv")
    quarterly_df.to_csv(quarterly_path, index=True)
    print(f"[Saved] {quarterly_path}")

    # --- 保存原始 companyfacts JSON ---
    if save_raw_json:
        fname_json = base + "_raw.json"
        with open(fname_json, "w") as f:
            json.dump(facts, f, indent=2)
        print(f"[Saved] {fname_json}")

    # --- 保存原始 CSV ---
    if save_raw_csv:
        raw_df = companyfacts_to_table(facts)
        fname_csv = base + "_raw.csv"
        raw_df.to_csv(fname_csv, index=False)
        print(f"[Saved] {fname_csv}")

    print(f"\nAll done. Files saved with base name: {base}\n")
    return hist, proj, meta

# CLI entry point: supports --ticker, --required, --perp, etc.
if __name__ == "__main__":
    main()