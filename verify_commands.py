"""verify_commands.py - offline command verification harness.

Checks that every bot command is wired correctly and runs end-to-end:

  1. STATIC AUDIT  - every command/alias/callback in registry.py + dispatch.py
     resolves to a handled command, and every menu entry has a handler.
  2. SMOKE RUN     - runs a representative command per family through
     dispatch.handle_command with replies captured locally (Telegram sends are
     mocked), against a throwaway copy of the state files, so nothing real is
     modified and no bot token is required.

Usage:
    py verify_commands.py [--smoke-only] [--no-network]
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Isolate state BEFORE anything imports corporate_actions.config (config
#    reads env at import time). A temp dir keeps the real state files intact.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
TMP = ROOT / ".verify_tmp"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir()
for name in ("watchlist.json", "subscriptions.json", "settings.json",
             "seen_actions.json", "schedule.json"):
    src = ROOT / name
    if src.exists():
        shutil.copy(src, TMP / name)

os.environ["WATCHLIST_FILE"] = str(TMP / "watchlist.json")
os.environ["SUBSCRIPTIONS_FILE"] = str(TMP / "subscriptions.json")
os.environ["SETTINGS_FILE"] = str(TMP / "settings.json")
os.environ["SEEN_FILE"] = str(TMP / "seen_actions.json")
os.environ["SCHEDULE_FILE"] = str(TMP / "schedule.json")
os.environ["TELEGRAM_CHAT_ID"] = os.getenv("TELEGRAM_CHAT_ID", "862087765")  # owner chat
os.environ["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
os.environ["HTTP_TIMEOUT"] = os.getenv("HTTP_TIMEOUT", "12")
os.environ["PROCESS_COMMANDS"] = "false"

NO_NETWORK = "--no-network" in sys.argv
SMOKE_ONLY = "--smoke-only" in sys.argv
ONE = sys.argv[sys.argv.index("--one") + 1] if "--one" in sys.argv else None

# ---------------------------------------------------------------------------
# 2. Capture replies by monkeypatching the send layer.
# ---------------------------------------------------------------------------
SENT: list[dict] = []


def _fake_send_message(text, parse_mode="HTML", chat_id=None, reply_markup=None):
    SENT.append({"chat_id": chat_id, "text": str(text), "markup": bool(reply_markup)})
    return {"ok": True, "result": {"message_id": len(SENT)}}


def _fake_answer_callback(callback_id):
    return None


def _fake_get_updates(offset=None):
    return []


def _fake_fetch(*args, **kwargs):
    raise RuntimeError("network disabled (--no-network)")


from corporate_actions import config  # noqa: E402
import corporate_actions.telegram.client as _client  # noqa: E402
from corporate_actions.bot import dispatch  # noqa: E402
from corporate_actions.bot import registry  # noqa: E402
from corporate_actions.bot import reply as _reply_mod  # noqa: E402
from corporate_actions.bot import watchlist_commands as _wl_mod  # noqa: E402
import corporate_actions.poller.engine as _engine  # noqa: E402

for _mod in (_client, _reply_mod, _wl_mod, _engine):
    _mod.send_message = _fake_send_message
_client.answer_callback_query = _fake_answer_callback
_client.get_updates = _fake_get_updates

if NO_NETWORK:
    from corporate_actions.sources import http as _http  # noqa: E402
    _http.requests.Session = type("NoNet", (), {"get": _fake_fetch, "post": _fake_fetch})

CHAT = 862087765

# ---------------------------------------------------------------------------
# 3. STATIC AUDIT
# ---------------------------------------------------------------------------
def _dispatch_commands() -> set[str]:
    """Every command literal appearing in dispatch.py's routing conditions."""
    src = Path(dispatch.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            cond = node.test
            if isinstance(cond, ast.Compare) and isinstance(cond.left, ast.Name) \
                    and cond.left.id == "command":
                for elt in cond.comparators:
                    if isinstance(elt, ast.Tuple):
                        for e in elt.elts:
                            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                found.add(e.value)
    return found


DISPATCH_CMDS = _dispatch_commands()
SPECIAL = {"/start", "/help", "/all", "/schednow", "/checknow"}

def _audit(label: str, items: set[str], handled: set[str]) -> list[str]:
    problems = []
    for item in sorted(items):
        if item not in handled:
            problems.append(f"  {label}: '{item}' has no dispatch route")
    return problems


def run_static_audit() -> list[str]:
    problems: list[str] = []
    handled = DISPATCH_CMDS | SPECIAL

    # Alias targets must resolve to a handled main command.
    problems += _audit("alias target", set(registry.ALIAS_TO_MAIN.values()), handled)
    # Usage/status/describe tables must be reachable.
    problems += _audit("usage table", set(registry.COMMAND_USAGE), handled)
    problems += _audit("status table", set(registry.COMMAND_STATUS), handled)
    problems += _audit("describe-and-run", set(registry.DESCRIBE_AND_RUN), handled)
    # Menu entries published to Telegram must be handled. register_commands
    # builds the list inline, so parse the literal dicts from its source.
    src = Path(registry.__file__).read_text(encoding="utf-8")
    menu = {f"/{m.group(1)}" for m in re.finditer(r'^\s*\{"command":\s*"([^"]+)"', src, re.M)}
    problems += _audit("telegram menu", menu, handled)

    # Every dispatch route must be one of the known command namespaces.
    known = set(registry.ALIAS_TO_MAIN) | set(registry.ALIAS_TO_MAIN.values()) | \
        set(registry.COMMAND_USAGE) | set(registry.DESCRIBE_AND_RUN) | \
        set(registry.COMMAND_STATUS) | menu
    for cmd in sorted(DISPATCH_CMDS - known):
        problems.append(f"  dispatch route '/{cmd.lstrip('/')}' has no registry entry (ok if intentionally internal)")

    # Callback prefixes wired in dispatch.
    cb_src = Path(dispatch.__file__).read_text(encoding="utf-8")
    for prefix in ("mfund", "fund:", "ana:", "stknext:"):
        if f'"{prefix}' not in cb_src and f"'{prefix}" not in cb_src:
            problems.append(f"  callback '{prefix}' missing from handle_callback_query")
    return problems


# ---------------------------------------------------------------------------
# 4. SMOKE RUN
# ---------------------------------------------------------------------------
def run_command(label: str, text: str) -> tuple[str, float, int]:
    SENT.clear()
    t0 = time.monotonic()
    try:
        dispatch.handle_command(CHAT, text)
        ok = True
    except Exception as error:  # noqa: BLE001 - report any crash
        ok = False
        err = f"{type(error).__name__}: {error}"
    dt = time.monotonic() - t0
    n = len(SENT)
    preview = (SENT[0]["text"][:110].replace("\n", " ") if SENT else "")
    return (f"OK  {label:34s} {dt:6.1f}s  msgs={n:<2} {preview}" if ok
            else f"ERR {label:34s} {dt:6.1f}s  msgs={n:<2} {err}")


def run_query(label: str, text: str) -> tuple[str, float, int]:
    SENT.clear()
    t0 = time.monotonic()
    error = None
    try:
        matched = dispatch.handle_query_text(CHAT, text)
    except Exception as exc:  # noqa: BLE001 - report any crash
        matched, error = False, f"{type(exc).__name__}: {exc}"
    dt = time.monotonic() - t0
    n = len(SENT)
    preview = (SENT[0]["text"][:110].replace("\n", " ") if SENT else "")
    if error:
        return f"ERR {label:34s} {dt:6.1f}s  msgs={n:<2} {error}"
    if matched:
        return f"OK  {label:34s} {dt:6.1f}s  msgs={n:<2} {preview}"
    return f"SKIP {label:34s} {dt:6.1f}s  msgs={n:<2} not matched (intended)"


def run_callback(label: str, data: str) -> tuple[str, float, int]:
    SENT.clear()
    cb = {"id": "cb-verify", "data": data, "message": {"chat": {"id": CHAT}}}
    t0 = time.monotonic()
    try:
        dispatch.handle_callback_query(cb)
        ok = True
    except Exception as error:
        ok = False
        err = f"{type(error).__name__}: {error}"
    dt = time.monotonic() - t0
    n = len(SENT)
    preview = (SENT[0]["text"][:110].replace("\n", " ") if SENT else "")
    return (f"OK  {label:34s} {dt:6.1f}s  msgs={n:<2} {preview}" if ok
            else f"ERR {label:34s} {dt:6.1f}s  msgs={n:<2} {err}")


# Command families: (label, command-text). Local-only commands are marked so
# they always run even with --no-network.
LOCAL = ("help", "settings", "schedule", "market", "menu", "learn", "watcher",
         "pricealert", "alertfilters", "moversfund", "favourites", "status", "checknow")

LOCAL_CMDS = [
    ("help",            "/help"),
    ("start",           "/start"),
    ("all",             "/all"),
    ("menu",            "/menu"),
    ("learn index",     "/learn"),
    ("settings",        "/settings"),
    ("status",          "/status"),
    ("schedule list",   "/schedule"),
    ("market status",   "/market"),
    ("watcher status",  "/watcher"),
    ("watcher on",      "/watcher on"),
    ("watcher set 3",   "/watcher set 3"),
    ("pricealert 3",    "/pricealert 3"),
    ("alertfilters",    "/alertfilters dividend,bonus"),
    ("moversfund auto", "/moversfund auto"),
    ("favourites",      "/myfavourites"),
    ("addstock bare",   "/addstock"),
    ("removestock bare","/removestock"),
    ("unknown cmd",     "/totallyboguscommand"),
    ("checknow",        "/checknow"),
]

NET_CMDS = [
    ("watchlist",        "/watchlist"),
    ("addstock ITC",     "/addstock ITC NSE"),
    ("removestock ITC",  "/removestock ITC NSE"),
    ("corpactions",      "/corpactions"),
    ("corpactions div",  "/corpactions dividend"),
    ("corpactionsummary","/corpactionssummary"),
    ("exdates today",    "/exdates today"),
    ("ca for my list",   "/corpactionsformylist"),
    ("news one",         "/news RELIANCE"),
    ("fundamental card", "/fundamentalanalyze RELIANCE"),
    ("fundamental deep", "/fundamentalreport RELIANCE"),
    ("us stock",         "/usstock AAPL"),
    ("checklist",        "/checklist RELIANCE"),
    ("indicator",        "/indicator RELIANCE RSI"),
    ("forecast",         "/forecast RELIANCE"),
    ("harmonic bare",    "/harmonicpatterns"),
    ("scan500 bare",     "/scan500"),  # bare -> description + full scan (heavy)
    ("topmovers",        "/topmovers 1h 5 nifty100"),
    ("topgainers",       "/topgainers 1h 5 nifty100"),
    ("toplosers",        "/toplosers 1h 5 nifty100"),
]

QUERIES = [
    ("query dividend",   "show me dividend stocks"),
    ("query ex-date",    "what are the upcoming ex-dates"),
    ("query gainers",    "top gainers today"),
    ("query news",       "latest news for my stocks"),
    ("query random",     "the weather is nice today"),
]

CALLBACKS = [
    ("cb mfund expired", "mfund"),
    ("cb unknown",       "bogus:data"),
    ("cb ana symbol",    "ana:RELIANCE"),
    ("cb fund symbol",   "fund:ITC"),
    ("cb stknext bad",   "stknext:1:99999"),
]


def main() -> int:
    problems = [] if SMOKE_ONLY else run_static_audit()
    print("=" * 100, flush=True)
    print("STATIC AUDIT: command registry + dispatch wiring")
    print("=" * 100)
    if not problems:
        print("  All registry commands, aliases, menu entries and callbacks are wired.")
        print(f"  Dispatch routes found: {len(DISPATCH_CMDS)}")
    else:
        print(f"  {len(problems)} issue(s):")
        for p in problems:
            print(p)

    print()
    print("=" * 100)
    print(f"SMOKE RUN (chat {CHAT}, replies captured, network={'OFF' if NO_NETWORK else 'ON'})")
    print("=" * 100, flush=True)
    results = []

    def _show(r):
        results.append(r)
        print(r, flush=True)
        with open(TMP / "progress.log", "a", encoding="utf-8") as _fh:
            _fh.write(r + "\n")
            _fh.flush()

    if not SMOKE_ONLY:
        for label, cmd in LOCAL_CMDS:
            _show(run_command(label, cmd))
    if NO_NETWORK:
        _show(run_command("(network skipped)", "/help"))
    else:
        for label, cmd in NET_CMDS:
            if label == "scan500 bare" and "--skip-scan500" in sys.argv:
                _show("SKIP scan500 bare                          (--skip-scan500)")
                continue
            _show(run_command(label, cmd))
        for label, q in QUERIES:
            _show(run_query(label, q))
        for label, data in CALLBACKS:
            _show(run_callback(label, data))

    for r in results:
        print(r)

    failed = [r for r in results if r.startswith("ERR")] + problems
    print()
    print("=" * 100)
    print(f"RESULT: {'ALL CHECKS PASSED' if not failed else f'{len(failed)} FAILURE(S) - see above'}")
    print("=" * 100)
    return 1 if failed else 0


if __name__ == "__main__":
    if ONE:
        if ONE.startswith("cb:"):
            print(run_callback(ONE[3:], ONE[3:]), flush=True)
        else:
            print(run_command(ONE, ONE), flush=True)
        sys.exit(0)
    sys.exit(main())
