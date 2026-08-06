"""Entry point for running in a GitHub Actions cron job.

Two jobs, one run:
  1. Process Telegram bot commands (/add, /remove, /list, /help) so the
     watchlist can be managed from Telegram. Any change is committed and
     pushed back to the repo using GH_TOKEN.
  2. Run one poll cycle: fetch corporate actions, filter to the watchlist,
     and send new ones to Telegram.

Local usage:  python run_bot.py
"""
import logging
import os
import subprocess
import sys

import requests

from corp_actions import config, notifier, sources, storage
from corp_actions.poller import poller

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("run_bot")

HELP_TEXT = (
    "Corporate Action Alerts bot.\n\n"
    "Commands:\n"
    "/add SYMBOL [NSE|BSE] - add a stock to the watchlist\n"
    "/remove SYMBOL [NSE|BSE] - remove a stock\n"
    "/list - show the watchlist\n"
    "/checknow - force a check and re-send all matching alerts\n"
    "/help - this message\n\n"
    "Examples:\n/add RELIANCE NSE\n/add PGINVIT NSE\n/remove TCS"
)


# --------------------------------------------------------------------- telegram
def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("result", [])


def reply(chat_id, text):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=config.HTTP_TIMEOUT)


# ----------------------------------------------------------------- watchlist
def handle_command(chat_id, text):
    parts = (text or "").strip().split()
    if not parts:
        return
    cmd = parts[0].lower()

    if cmd in ("/start", "/help"):
        reply(chat_id, HELP_TEXT)
        return

    if cmd == "/list":
        items = storage.get_user_list(chat_id)
        if not items:
            reply(chat_id, "Your watchlist is empty.")
        else:
            lines = [f"{i['symbol']} ({i['exchange']})" for i in items]
            reply(chat_id, "Your watchlist:\n" + "\n".join(lines))
        return

    if cmd == "/checknow":
        reply(chat_id, "Running a forced check now - re-sending all matching alerts shortly.")
        return

    if len(parts) < 2:
        reply(chat_id, "Usage: /add SYMBOL [NSE|BSE]  or  /remove SYMBOL [NSE|BSE]")
        return

    symbol = parts[1].upper()
    exchange = (parts[2].upper() if len(parts) > 2 else "NSE")
    exchange = exchange if exchange in ("NSE", "BSE") else "NSE"

    if cmd == "/add":
        quote = sources.get_quote(exchange, symbol)
        if quote is None:
            _reply_suggestions(chat_id, symbol)
            return
        storage.add_to_user_list(
            chat_id,
            {"symbol": symbol, "company": quote.get("name", ""), "exchange": exchange},
        )
        reply(chat_id, f"Added {symbol} ({exchange}). Alerts will come to this chat.")
    elif cmd == "/remove":
        storage.remove_from_user_list(chat_id, symbol, exchange)
        reply(chat_id, f"Removed {symbol} ({exchange}) if it was present.")
    else:
        reply(chat_id, HELP_TEXT)


def _reply_suggestions(chat_id, query):
    """Reply with matching stocks from the NSE list when an exact symbol fails."""
    matches = sources.search_stocks(query, limit=10)
    if not matches:
        reply(chat_id, f"No stocks match '{query}'.")
        return
    lines = [f"'{query}' not found as an exact symbol. Did you mean (NSE):"]
    for m in matches:
        company = m["company"] or ""
        lines.append(f"  /add {m['symbol']} NSE  - {company}")
    reply(chat_id, "\n".join(lines))


def process_commands():
    """Process any pending Telegram command updates.

    Returns the chat_id that requested /checknow, or None.
    """
    if not notifier.is_configured():
        return None
    try:
        updates = get_updates()
    except requests.RequestException as exc:
        log.warning("getUpdates failed: %s", exc)
        return None

    checknow_chat = None
    max_offset = 0
    for update in updates:
        update_id = update.get("update_id", 0)
        max_offset = max(max_offset, update_id)
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if not text.startswith("/"):
            continue
        if text.strip().lower() == "/checknow":
            checknow_chat = str(chat_id)
        handle_command(chat_id, text)
    if max_offset:
        # Mark updates as consumed.
        get_updates(offset=max_offset + 1)
    return checknow_chat


# ----------------------------------------------------------------------- git
def _remote_default_branch(remote_url) -> str:
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--symref", remote_url, "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout
        for line in out.splitlines():
            if line.strip().startswith("ref:"):
                return line.split()[-1].rsplit("/", 1)[-1]
    except Exception:
        pass
    return ""


def push_state():
    """Commit and push watchlist/seen state back to the repo, if changed."""
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        log.info("GH_TOKEN/GITHUB_REPOSITORY not set - skipping push")
        return
    subprocess.run(["git", "config", "user.email", "actions@github.com"], check=False)
    subprocess.run(["git", "config", "user.name", "github-actions"], check=False)
    subprocess.run(
        ["git", "add", str(config.WATCHLIST_FILE), str(config.SEEN_FILE), str(config.SUBSCRIPTIONS_FILE)],
        check=False,
    )
    has_diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], check=False
    ).returncode != 0
    if not has_diff:
        log.info("No state change to push")
        return
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    branch = os.getenv("GH_PUSH_BRANCH") or ""
    if not branch:
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    if not branch:
        branch = _remote_default_branch(remote_url)
    branch = branch or "main"
    subprocess.run(["git", "commit", "-m", "chore: update watchlist from Telegram"], check=False)
    push = subprocess.run(
        ["git", "push", remote_url, f"HEAD:{branch}"],
        capture_output=True, text=True, check=False,
    )
    if push.returncode == 0:
        log.info("Pushed state to %s", branch)
    else:
        log.warning("Push failed: %s", push.stderr.strip()[-500:])


# ------------------------------------------------------------------------- main
def main():
    log.info("Processing Telegram commands...")
    checknow_chat = process_commands()
    log.info("Running poll cycle%s...", f" (forced for {checknow_chat})" if checknow_chat else "")
    sent = poller.run_once(force=bool(checknow_chat), only_chat=checknow_chat)
    log.info("Pushing state if changed...")
    push_state()
    log.info("Done. Sent %s alert(s).", sent)


if __name__ == "__main__":
    main()
