"""Snapshot/verdict analysis as plain data (powers the web /api/analysis).

Reuses the exact rating rules from the Telegram deep report
(`formatting.stock_india_report`) so the web Snapshot & Verdict card can
never drift from the bot's verdict — but returns JSON-safe plain data
instead of Telegram HTML. No fetching here; fund/quote dicts are passed in.
"""
from __future__ import annotations

from .formatting.stock_common import _holding_delta
from .formatting.stock_india_report import (
    _fy_label,
    _main_question,
    _overall_ratings,
    _verdict,
)


def _fmt_num(value, decimals: int = 1) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def build_analysis(fund: dict | None, price=None) -> dict:
    """Snapshot rows, verdict, concerns, positives and the main question."""
    fund = fund or {}
    ratings = _overall_ratings(fund, price)

    snapshot = [
        {"label": label, "icon": icon, "word": word}
        for label, rating in ratings.items()
        if rating for icon, word in [rating]
    ]
    verdict_text = _verdict(ratings)
    verdict_icon = (verdict_text or "").split()[0] if verdict_text else ""

    concerns: list[str] = []
    if fund.get("pe") is not None:
        try:
            if float(fund["pe"]) > 40:
                concerns.append(f"P/E {_fmt_num(fund['pe'])}x — expensive vs earnings")
        except (TypeError, ValueError):
            pass
    if fund.get("roe") is not None:
        try:
            if float(fund["roe"]) < 10:
                concerns.append(f"ROE only {_fmt_num(fund['roe'])}%")
        except (TypeError, ValueError):
            pass
    if fund.get("roce") is not None:
        try:
            if float(fund["roce"]) < 12:
                concerns.append(f"ROCE only {_fmt_num(fund['roce'])}%")
        except (TypeError, ValueError):
            pass
    cash_flow = fund.get("cash_flow") or {}
    if cash_flow.get("free_cash_flow") is not None:
        try:
            if float(cash_flow["free_cash_flow"]) < 0:
                concerns.append("Negative free cash flow")
        except (TypeError, ValueError):
            pass
    if cash_flow.get("cfo") is not None:
        try:
            if float(cash_flow["cfo"]) < 0:
                concerns.append("Operating cash flow (CFO) is negative")
        except (TypeError, ValueError):
            pass
    if fund.get("interest_coverage_ratio") is not None:
        try:
            if float(fund["interest_coverage_ratio"]) < 2.5:
                concerns.append(
                    f"Interest coverage only {_fmt_num(fund['interest_coverage_ratio'])}x"
                )
        except (TypeError, ValueError):
            pass
    if price is not None and fund.get("sma_50") is not None and fund.get("sma_200") is not None:
        try:
            if float(price) < float(fund["sma_50"]) and float(price) < float(fund["sma_200"]):
                concerns.append("Price below both 50-day and 200-day averages")
        except (TypeError, ValueError):
            pass
    if (fund.get("macd_line") is not None and fund.get("macd_signal") is not None):
        try:
            if float(fund["macd_line"]) < float(fund["macd_signal"]):
                concerns.append("Bearish MACD (line below signal)")
        except (TypeError, ValueError):
            pass
    fii_delta = _holding_delta(fund.get("fii_pct"))
    if fii_delta is not None and fii_delta < 0:
        concerns.append(f"FII holding declined {abs(fii_delta):.2f}% QoQ")

    positives: list[str] = []
    annuals = fund.get("annuals") or []
    latest = annuals[-1] if annuals else None
    if latest:
        if latest.get("sales_growth") is not None and latest["sales_growth"] > 0:
            positives.append(f"Latest FY sales +{latest['sales_growth']:.1f}% YoY")
        if latest.get("profit_growth") is not None and latest["profit_growth"] > 0:
            positives.append(f"Latest FY profit +{latest['profit_growth']:.1f}% YoY")
    if len(annuals) >= 3 and all(item.get("sales") is not None for item in annuals):
        try:
            if float(annuals[-1]["sales"]) > float(annuals[0]["sales"]):
                positives.append("Multi-year sales expansion")
        except (TypeError, ValueError):
            pass
    if fund.get("debt_to_equity") is not None:
        try:
            if float(fund["debt_to_equity"]) <= 0.5:
                positives.append(f"Low debt (D/E {float(fund['debt_to_equity']):.2f}x)")
        except (TypeError, ValueError):
            pass
    dii_delta = _holding_delta(fund.get("dii_pct"))
    if dii_delta is not None and dii_delta > 0:
        positives.append("DII holding increased QoQ")
    try:
        from .formatting.stock_common import _consensus_label

        if _consensus_label(fund) in ("Buy", "Strong Buy"):
            positives.append("Analyst consensus is Buy")
    except Exception:
        pass
    if fund.get("target_mean") is not None and price:
        try:
            if float(fund["target_mean"]) > float(price):
                upside = (float(fund["target_mean"]) - float(price)) / float(price) * 100
                positives.append(f"Mean target implies +{upside:.0f}% upside")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    try:
        main_question = _main_question(fund, price)
    except Exception:
        main_question = ["Is the current growth rate sustainable over the next few years?"]

    return {
        "snapshot": snapshot,
        "verdict": verdict_text,
        "verdict_icon": verdict_icon,
        "concerns": concerns,
        "positives": positives,
        "main_question": main_question,
    }
