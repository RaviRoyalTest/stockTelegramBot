"""Entry point for running in a GitHub Actions cron job.

Two jobs, one run:
  1. Optionally process Telegram bot commands (/add, /remove, /list, /help)
     when PROCESS_COMMANDS=true (default). Set PROCESS_COMMANDS=false in the
     GitHub Actions cron so the always-on bot server is the only process that
     polls getUpdates (avoids double replies and 409 conflicts). Any change
     is committed and pushed back to the repo using GH_TOKEN.
  2. Run one poll cycle: fetch corporate actions, filter to the watchlist,
     and send new ones to Telegram.

Local usage:  python run_bot.py
"""
import logging
import os
import subprocess
import sys

import requests

import corp_actions.poller as poller_mod
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
    "/next - show upcoming ex-dates for your watchlist\n"
    "/filter TYPE,TYPE - only receive these action types\n"
    "   types: dividend, bonus, split, rights, buyback (or /filter all)\n"
    "/alert PCT - alert me when a stock moves +/-PCT% in a day (/alert off)\n"
    "/status - show where your list is saved and if GitHub push is on\n"
    "/checknow - force a check and re-send all matching alerts\n"
    "/help - this message\n\n"
    "Examples:\n/add RELIANCE NSE\n/add PGINVIT NSE\n/remove TCS\n"
    "/filter dividend,bonus\n/alert 3"
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
            where = (
                "watchlist.json (owner's list)"
                if storage.is_owner(chat_id)
                else f"subscriptions.json (your chat {chat_id})"
            )
            reply(
                chat_id,
                "Your watchlist:\n"
                + "\n".join(lines)
                + f"\n\nSaved in: {where} - pushed to GitHub so it survives redeploys.",
            )
        return

    if cmd == "/checknow":
        reply(chat_id, "Running a forced check now - re-sending all matching alerts shortly.")
        return

    if cmd == "/next":
        items = storage.get_user_list(chat_id)
        if not items:
            reply(chat_id, "Your watchlist is empty.")
            return
        try:
            matching = poller_mod.fetch_matching(items)
        except Exception as exc:
            reply(chat_id, f"Could not fetch corporate actions: {exc}")
            return
        upcoming = [
            a for a in matching if poller_mod.within_reminder_window(a.get("ex_date"))
        ]
        reply(chat_id, notifier.format_upcoming_list(upcoming))
        return

    if cmd == "/filter":
        settings = storage.get_user_settings(chat_id)
        current = settings.get("action_filters") or []
        if len(parts) < 2:
            reply(
                chat_id,
                "Current filters: " + (", ".join(current) if current else "all types")
                + "\nUsage: /filter dividend,bonus  or  /filter all",
            )
            return
        raw = parts[1].lower()
        bad = []
        if raw in ("all", "off", "none", "-"):
            chosen = []
        else:
            chosen = []
            for token in raw.split(","):
                token = token.strip()
                if token in sources.ACTION_TYPES:
                    chosen.append(token)
                elif token:
                    bad.append(token)
        settings["action_filters"] = chosen
        storage.save_user_settings(chat_id, settings)
        msg = "Filters set to: " + (", ".join(chosen) if chosen else "all types")
        if bad:
            msg += f"\nIgnored unknown type(s): {', '.join(bad)}"
            msg += f" (valid: {', '.join(sources.ACTION_TYPES)})"
        reply(chat_id, msg)
        return

    if cmd == "/alert":
        settings = storage.get_user_settings(chat_id)
        current = settings.get("price_alert_pct")
        if len(parts) < 2:
            if current:
                reply(chat_id, f"Current price-alert threshold: {current:g}%")
            else:
                reply(chat_id, "Price alerts are off.\nUsage: /alert 3  (percent move)  or  /alert off")
            return
        raw = parts[1].lower()
        if raw in ("off", "none", "0", "0%"):
            val = None
        else:
            try:
                val = abs(float(raw.strip().rstrip("%")))
            except ValueError:
                reply(chat_id, "Usage: /alert 3  (e.g. 3%)  or  /alert off")
                return
            if val == 0:
                val = None
        settings["price_alert_pct"] = val
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, f"Price alerts {'off' if val is None else 'set to ' + format(val, 'g') + '%'}.")
        return

    if cmd == "/status":
        gh_configured = bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))
        owner = storage.is_owner(chat_id)
        location = (
            "watchlist.json (the owner's list)"
            if owner
            else f"subscriptions.json (your chat {chat_id})"
        )
        push_status = (
            "configured - your changes are pushed to GitHub after each command"
            if gh_configured
            else "NOT set - your changes stay only on this host's disk (lost on redeploy)"
        )
        reply(
            chat_id,
            "\n".join(
                [
                    f"Your chat id: {chat_id}",
                    f"Role: {'owner' if owner else 'subscriber'}",
                    f"Your list is saved in: {location}",
                    f"GitHub push: {push_status}",
                    "Run /list to see your current watchlist.",
                ]
            ),
        )
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
    if not config.PROCESS_COMMANDS:
        log.info("PROCESS_COMMANDS=false - skipping Telegram command processing")
        return None
    if not notifier.is_configured():
        return None
    try:
        updates = get_updates()
    except requests.RequestException as exc:
        log.warning("getUpdates failed: %s", config.redact(exc))
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


def _push_branch(remote_url: str) -> str:
    """Resolve the branch that state is pushed to / synced from."""
    branch = os.getenv("GH_PUSH_BRANCH") or ""
    if not branch:
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    if not branch:
        branch = _remote_default_branch(remote_url)
    return branch or "main"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def _ahead_of_origin(branch: str) -> bool:
    """True when the local branch has commits not present on origin/{branch}.

    This is the signal that a previous commit was never pushed - pushing
    again is required; a hard reset at this point would destroy data.
    """
    res = _git("git", "rev-list", "--count", f"origin/{branch}..HEAD")
    if res.returncode != 0:
        return False
    try:
        return int(res.stdout.strip()) > 0
    except ValueError:
        return False


def push_state() -> bool:
    """Commit and push watchlist/seen state back to the repo, if changed.

    Returns True when the repo is in sync (pushed, or nothing to push).
    Returns False when credentials are missing or the push failed - callers
    should NOT discard local state in that case.

    Handles the expected race with the hourly cron (both push to the same
    branch): on a rejected push it fetches, rebases onto the remote and
    retries once.
    """
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        log.warning(
            "GH_TOKEN/GITHUB_REPOSITORY not set - skipping push. State is "
            "only on this host's disk and WILL BE LOST on redeploy. Set "
            "GH_TOKEN (fine-grained PAT, Contents: Read and write) and "
            "GITHUB_REPOSITORY (e.g. RaviRoyalTest/stockTelegramBot) in the "
            "host environment."
        )
        return False
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    branch = _push_branch(remote_url)

    _git("git", "config", "user.email", "actions@github.com")
    _git("git", "config", "user.name", "github-actions")
    added = _git(
        "git",
        "add",
        str(config.WATCHLIST_FILE),
        str(config.SEEN_FILE),
        str(config.SUBSCRIPTIONS_FILE),
        str(config.SETTINGS_FILE),
    )
    if added.returncode != 0:
        log.warning(
            "git add failed - state NOT pushed (local changes kept): %s",
            added.stderr.strip()[-300:],
        )
        return False
    if _git("git", "diff", "--cached", "--quiet").returncode == 0:
        # Nothing staged. But there may be local commits from a previous run
        # that failed to push. If we are ahead of origin, retry the push
        # instead of claiming "in sync" - otherwise a later sync_state()'s
        # reset --hard would silently destroy those commits.
        if _ahead_of_origin(branch):
            push = _git("git", "push", remote_url, f"HEAD:{branch}")
            if push.returncode == 0:
                log.info("Pushed previously-unpushed state to %s", branch)
                return True
            log.warning(
                "Retry push of existing local commits failed: %s",
                push.stderr.strip()[-300:],
            )
            return False
        log.info("No state change to push")
        return True

    commit = _git("git", "commit", "-m", "chore: update watchlist from Telegram")
    if commit.returncode != 0:
        # Keep the changes in the worktree instead of the index so a later
        # sync (reset --hard) refuses to wipe them.
        log.warning("State commit failed: %s", commit.stderr.strip()[-300:])
        _git("git", "reset")
        return False

    push = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push.returncode == 0:
        log.info("Pushed state to %s", branch)
        return True

    # Expected race with the cron: retry once after rebasing onto remote.
    _git("git", "fetch", "origin")
    rebase = _git("git", "rebase", f"origin/{branch}")
    if rebase.returncode != 0:
        _git("git", "rebase", "--abort")
        log.warning(
            "Push failed and rebase aborted (conflict): %s",
            push.stderr.strip()[-300:],
        )
        return False
    push2 = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push2.returncode == 0:
        log.info("Pushed state to %s (after rebase)", branch)
        return True
    log.warning("Push failed after rebase: %s", push2.stderr.strip()[-500:])
    return False


def sync_state() -> bool:
    """Pull the latest committed state from GitHub before handling commands.

    GitHub is the source of truth; an always-on server's local copy is just a
    working checkout whose disk is ephemeral. Sync before serving commands so
    the server never answers with stale data or overwrites newer state.
    Never resets when the working tree has uncommitted changes (a failed push
    from a previous run) - that would wipe data.
    Returns True when synced or skipped safely (no credentials / dirty tree).
    """
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        log.info("GH_TOKEN/GITHUB_REPOSITORY not set - skipping state sync")
        return True
    try:
        remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        branch = _push_branch(remote_url)
        dirty = _git("git", "status", "--porcelain").stdout.strip()
        if dirty:
            log.warning(
                "State sync skipped: uncommitted changes present - push them "
                "first (dirty: %s)",
                dirty[:200],
            )
            return True
        _git("git", "fetch", "origin")
        if _ahead_of_origin(branch):
            # Local commits exist that were never pushed. A hard reset here
            # would silently destroy them - push them first instead.
            log.warning(
                "State sync skipped: local branch is ahead of origin/%s "
                "(unpushed commits). Run push_state or fix credentials first.",
                branch,
            )
            return True
        res = _git("git", "reset", "--hard", f"origin/{branch}")
        if res.returncode == 0:
            log.info("State synced from origin/%s", branch)
            return True
        log.warning("State sync failed: %s", res.stderr.strip()[-300:])
        return False
    except Exception as exc:
        log.warning("State sync failed: %s", exc)
        return False


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
