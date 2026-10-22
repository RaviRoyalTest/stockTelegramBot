"""Small helpers shared by several command families."""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..core.text import escape
from ..sources import (
    get_bse_stock_list,
    get_nse_stock_list_cached,
    get_quote,
    search_stocks,
    search_us_tickers,
)
from .reply import reply

log = logging.getLogger(__name__)

MAX_QUERY_ITEMS = 20  # entries per message batch
MAX_NEWS_STOCKS = 10  # stocks processed by /news per request
MAX_STOCK_BATCH = 10
MAX_FUND_BATCH = 5


def run_command_sequence(chat_id, commands: list[str], intro: str,
                         done: str | None = None, source_note: str | None = None) -> None:
    """Run a list of commands with a labelled intro and per-command isolation.

    Shared by /myfavourites run and /schedule run (and /schednow): sends the
    intro + the commands being run, executes each one (a failing command must
    never stop the rest), and optionally closes with a completion line. The
    source_note names the watchlist the results relate to, so automatic runs
    are never anonymous.
    """
    if source_note:
        intro += f"\nSource list: <code>{html.escape(source_note)}</code>"
    reply(
        chat_id,
        intro + "\n" + "\n".join(
            f"  \u2022 <code>{html.escape(command)}</code>" for command in commands
        ),
    )
    for command in commands:
        try:
            log.info("run sequence: executing %s (chat %s)", command, chat_id)
            from .dispatch import handle_command  # late import: breaks the module cycle
            handle_command(chat_id, command)
        except Exception as error:
            log.warning(
                "run sequence: command %s failed: %s",
                command, config.redact(error), exc_info=True,
            )
            try:
                reply(
                    chat_id,
                    f"<code>{html.escape(command)}</code> failed: "
                    f"{html.escape(config.redact(str(error)))}",
                )
            except Exception as error:
                log.warning("run_command_sequence: %s", error)
                pass
    if done:
        reply(chat_id, done)


def attach_quotes(actions: list[dict], max_workers: int = 6) -> None:
    """Fetch current prices for the actions in parallel and attach them."""
    if not actions:
        return
    for action in actions:
        action.pop("quote", None)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(
            executor.map(
                lambda action: (action, get_quote(action["exchange"], action["symbol"])),
                list(actions),
            )
        )
    attached = 0
    for action, quote in results:
        if quote:
            action["quote"] = quote
            attached += 1
    log.info(
        "attach_quotes: %d/%d quotes fetched", attached, len(actions)
    )


def close_symbols(query: str, limit: int = 3) -> list[str]:
    """Fuzzy NSE symbol suggestions via difflib (e.g. 'gensys' -> GENESYS).

    Exact substring search fails on typos and symbol-vs-company-name
    mismatches, so fall back to close matches from the full NSE list.
    """
    try:
        from difflib import get_close_matches

        stocks = get_nse_stock_list_cached()
        symbols = [stock["symbol"] for stock in stocks]
    except Exception as error:
        log.warning("close_symbols: %s", error)
        return []
    return get_close_matches((query or "").upper(), symbols, n=limit, cutoff=0.72)


def resolve_or_none(symbol: str, exchange: str = "NSE") -> dict | None:
    """Validate one symbol and return {'symbol','company','exchange'} or None.

    Same validation ladder as handle_add_remove: Yahoo quote first, then the
    exact-symbol pass through the exchange's own stock list / US ticker search.
    """
    raw = str(symbol or "").strip().upper()
    if not raw:
        return None
    if raw.endswith(".NS"):
        raw, exchange = raw[:-3], "NSE"
    elif raw.endswith(".BO"):
        raw, exchange = raw[:-3], "BSE"
    quote = get_quote(exchange, raw)
    if quote:
        return {"symbol": raw, "company": quote.get("name", "") or "", "exchange": exchange}
    try:
        if exchange == "NSE":
            exact = next(
                (s for s in search_stocks(raw, limit=5) if s["symbol"].upper() == raw),
                None,
            )
            if exact:
                return {"symbol": raw, "company": exact.get("company", "") or "", "exchange": exchange}
        elif exchange == "BSE":
            bse_list = get_bse_stock_list()
            exact = next(
                (s for s in bse_list if s["symbol"].upper() == raw or s.get("code") == raw),
                None,
            )
            if exact:
                return {"symbol": exact["symbol"], "company": exact.get("company", "") or "", "exchange": exchange}
        elif exchange == "US":
            us_matches = search_us_tickers(raw, limit=1)
            if us_matches:
                exact = us_matches[0]
                return {
                    "symbol": exact.get("symbol", raw),
                    "company": exact.get("name") or exact.get("company", "") or "",
                    "exchange": exchange,
                }
    except Exception as error:
        log.warning("resolve_or_none(%s:%s) fallback failed: %s", exchange, raw, error)
    return None


_NSE_NAME_CACHE: dict[str, str] = {}

# Fields that prove a lookup really found a stock (rather than a made-up
# ticker Yahoo's chart endpoint happily answers with an empty shell).
_STOCK_PROOF_FIELDS = (
    "pe", "market_cap", "mcap_cr", "wk52_high", "wk52_low", "sector",
    "book_value", "trailing_eps", "div_yield", "price",
)


def _norm_symbol(text) -> str:
    """Alphanumeric-only uppercase form, for tolerant symbol comparison."""
    return "".join(char for char in str(text or "").upper() if char.isalnum())


def _nse_company_names() -> dict[str, str]:
    """{SYMBOL: company name} from the cached NSE list (built once)."""
    global _NSE_NAME_CACHE
    if not _NSE_NAME_CACHE:
        try:
            _NSE_NAME_CACHE = {
                str(stock.get("symbol", "")).upper(): stock.get("company", "") or ""
                for stock in get_nse_stock_list_cached()
                if stock.get("symbol")
            }
        except Exception as error:
            log.warning("company name map unavailable: %s", error)
    return _NSE_NAME_CACHE


def has_stock_data(symbol: str, quote: dict | None, fund: dict | None) -> bool:
    """True when a lookup found a real stock instead of an empty shell.

    Yahoo's chart endpoint answers for ANY ticker, so a made-up symbol comes
    back as ``{'company': '<THE SYMBOL>', ...indicators}`` with no price and
    no ratios. Treating that as a stock is what printed blank reports instead
    of a 'did you mean' pick-list, so every command gates on this.
    """
    quote = quote or {}
    fund = fund or {}
    if quote.get("price") is not None:
        return True
    if any(fund.get(field) is not None for field in _STOCK_PROOF_FIELDS):
        return True
    company = str(fund.get("company") or fund.get("name") or "").strip()
    return bool(company) and _norm_symbol(company) != _norm_symbol(symbol)


def symbol_candidates(query: str, market: str = "any", limit: int = 6) -> list[dict]:
    """Candidate stocks for a mistyped symbol/company: ticker + full name.

    NSE substring/fuzzy search first, then difflib-close NSE symbols with
    their company names, then Yahoo's US ticker search. An exact ticker match
    is always listed first so 'Try:' never suggests something unrelated.
    """
    query = (query or "").strip()
    if not query:
        return []
    market = (market or "any").lower()
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(symbol, company, exchange):
        symbol = str(symbol or "").upper().strip()
        exchange = str(exchange or "NSE").upper()
        if not symbol or (symbol, exchange) in seen:
            return
        seen.add((symbol, exchange))
        candidates.append({"symbol": symbol, "company": company or "", "exchange": exchange})

    if market in ("any", "in"):
        try:
            for match in search_stocks(query, limit=limit):
                add(match.get("symbol"), match.get("company"), "NSE")
        except Exception as error:
            log.warning("symbol_candidates: NSE search failed: %s", error)
        if len(candidates) < limit:
            names = _nse_company_names()
            for symbol in close_symbols(query, limit=limit):
                add(symbol, names.get(symbol.upper(), ""), "NSE")
    if market in ("any", "us") and len(candidates) < limit:
        try:
            for match in search_us_tickers(query, limit=limit):
                add(
                    match.get("symbol"),
                    match.get("name") or match.get("company"),
                    match.get("exchange") or "US",
                )
        except Exception as error:
            log.warning("symbol_candidates: US search failed: %s", error)

    exact = _norm_symbol(query)
    candidates.sort(key=lambda candidate: 0 if _norm_symbol(candidate["symbol"]) == exact else 1)
    return candidates[:limit]


def known_symbol(query: str, market: str = "any") -> bool:
    """True when the query IS an exact ticker on NSE (or Yahoo for US).

    Lets a 'no data' path tell a typo (suggest names) apart from a real
    ticker whose feed is having a bad minute (ask them to retry).
    """
    target = _norm_symbol(query)
    if not target:
        return False
    if market in ("any", "in"):
        if any(_norm_symbol(symbol) == target for symbol in _nse_company_names()):
            return True
    if market in ("any", "us"):
        try:
            if any(_norm_symbol(match.get("symbol")) == target
                   for match in search_us_tickers(query, limit=6)):
                return True
        except Exception as error:
            log.warning("known_symbol: US search failed: %s", error)
    return False


def not_found_message(
    query: str,
    candidates: list[dict],
    command: str = "fundamentalreport",
    args: str = "",
    market: str = "any",
) -> str:
    """'Did you mean' text: up to 6 ticker + company names, then a Try line.

    Always returns something actionable, even when no candidate is found, so
    every stock command can hand it straight to the user.
    """
    query_text = escape(query)
    exact = [c for c in candidates if _norm_symbol(c["symbol"]) == _norm_symbol(query)]
    if exact:
        return (
            f"\u26a0\ufe0f <code>{query_text}</code> is a valid ticker but live data "
            "didn't load right now.\n"
            f"Try again in a minute: <code>/{command} {escape(exact[0]['symbol'])}{args}</code>"
        )
    lines = [f"\u2753 <b>No stock matched</b> <code>{query_text}</code>"]
    if candidates:
        lines.append("")
        lines.append("Did you mean one of these?")
        for candidate in candidates:
            company = escape(candidate.get("company") or "")
            suffix = f" \u2014 {company}" if company else ""
            lines.append(
                f"\u2022 <code>{escape(candidate['symbol'])}</code>{suffix} "
                f"({escape(candidate['exchange'])})"
            )
        lines.append("")
        lines.append(f"Try: <code>/{command} {escape(candidates[0]['symbol'])}{args}</code>")
    else:
        scope = {
            "in": "NSE/BSE",
            "us": "the US market",
        }.get(market, "NSE/BSE or the US market")
        lines.append("")
        lines.append(f"I couldn't find anything close on {scope}.")
        lines.append(
            "Check the spelling, or search by company name "
            f"(e.g. <code>/{command} reliance</code>)."
        )
    return "\n".join(lines)


def reply_suggestions(chat_id, query, command="add", args="", market="any"):
    """Suggest close tickers (ticker + full company name) when a lookup fails.

    The 'Try:' line reuses the command the user actually ran
    (add|fundamentalanalyze|fundamentalreport|harmonic|checklist|forecast|
    indicator|gappers|news) so tapping it runs the right follow-up.
    """
    if command in ("add", "/add", "addstock", "/addstock"):
        command, args = "addstock", args or " NSE"
    command = command.lstrip("/")
    candidates = symbol_candidates(query, market=market)
    log.info(
        "suggestions for %r (chat %s): %d candidate(s)",
        query, chat_id, len(candidates),
    )
    reply(chat_id, not_found_message(query, candidates, command, args, market))
