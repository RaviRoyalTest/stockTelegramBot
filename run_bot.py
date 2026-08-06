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
from pathlib import Path

from corp_actions import config  # no third-party deps - always importable

try:
    import requests

    import corp_actions.poller as poller_mod
    from corp_actions import notifier, sources, storage
    from corp_actions.poller import poller
except ImportError:
    # The dependency-light --check diagnostic must still run when
    # requirements.txt hasn't been installed yet. Anything that actually
    # needs the missing deps fails later with a clear error.
    if not any(a.lower() == "--check" for a in sys.argv[1:]):
        raise
    print(
        "Note: some dependencies are missing - running in dependency-light "
        "diagnostic mode. Install them for the full bot: "
        "pip install -r requirements.txt",
        file=sys.stderr,
    )


class _ImmediateStreamHandler(logging.StreamHandler):
    """Flush after every record so Render / PaaS logs appear immediately.

    When stdout is piped (not a TTY - the norm on Render), Python enables
    block buffering, so logs written with the default StreamHandler sit in
    the buffer and Render shows nothing for a long time. Flushing on every
    emit makes each log line appear in the dashboard right away.
    """

    def emit(self, record):
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    handlers=[_ImmediateStreamHandler(sys.stdout)],
)
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
    "/help - this message\n"
    "/start - this message\n"
    "(tip: type / alone to see this help)\n\n"
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


def github_push_configured() -> bool:
    """True only when the host can actually push state back to GitHub."""
    return bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))


# ----------------------------------------------------------------- watchlist
def handle_command(chat_id, text):
    parts = (text or "").strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    log.info("command from chat %s: %s", chat_id, text)

    if cmd in ("/start", "/help", "/"):
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
            persistence = (
                "pushed to GitHub - it survives redeploys."
                if github_push_configured()
                else "NOT pushed to GitHub - it is only on this host's disk "
                "and WILL BE LOST on redeploy. Run /status to confirm."
            )
            reply(
                chat_id,
                "Your watchlist:\n"
                + "\n".join(lines)
                + f"\n\nSaved in: {where} - {persistence}",
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
        log.info(
            "chat %s filters set to: %s",
            chat_id, ", ".join(chosen) if chosen else "all types",
        )
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
        log.info(
            "chat %s price-alert threshold set to: %s",
            chat_id, "off" if val is None else f"{val:g}%",
        )
        reply(chat_id, f"Price alerts {'off' if val is None else 'set to ' + format(val, 'g') + '%'}.")
        return

    if cmd == "/status":
        gh_configured = github_push_configured()
        owner = storage.is_owner(chat_id)
        location = (
            "watchlist.json (the owner's list)"
            if owner
            else f"subscriptions.json (your chat {chat_id})"
        )
        if gh_configured:
            branch = _push_branch("")
            pending = pending_state_changes()
            push_status = (
                f"configured - changes are pushed to GitHub (branch {branch}) "
                "after each command"
            )
            sync_line = (
                "Local state vs GitHub: "
                + (pending or "in sync (nothing uncommitted)")
            )
        else:
            push_status = (
                "NOT set - your changes stay only on this host's disk (lost "
                "on redeploy). Set GH_TOKEN + GITHUB_REPOSITORY on this host."
            )
            sync_line = "Local state vs GitHub: unknown (no GitHub credentials)"
        reply(
            chat_id,
            "\n".join(
                [
                    f"Your chat id: {chat_id}",
                    f"Role: {'owner' if owner else 'subscriber'}",
                    f"Your list is saved in: {location}",
                    f"GitHub push: {push_status}",
                    sync_line,
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
        company = quote.get("name", "") if quote else ""
        validated = quote is not None
        if not validated and exchange == "NSE":
            # Yahoo can be flaky from datacenter IPs (e.g. Render). Fall back
            # to the NSE stock list so valid tickers still get added even when
            # the live quote is unavailable.
            exact = next(
                (
                    s for s in sources.search_stocks(symbol, limit=5)
                    if s["symbol"].upper() == symbol
                ),
                None,
            )
            if exact is not None:
                company = exact["company"]
                validated = True
                log.info(
                    "Yahoo quote unavailable for %s:%s; validated via NSE stock list",
                    exchange, symbol,
                )
        if not validated:
            _reply_suggestions(chat_id, symbol)
            return
        storage.add_to_user_list(
            chat_id,
            {"symbol": symbol, "company": company, "exchange": exchange},
        )
        where = (
            "watchlist.json (owner's list)"
            if storage.is_owner(chat_id)
            else f"subscriptions.json (chat {chat_id})"
        )
        log.info("Added %s (%s) for chat %s -> %s", symbol, exchange, chat_id, where)
        reply(
            chat_id,
            f"Added {symbol} ({exchange}). Alerts will come to this chat.\n"
            f"Saved in: {where}.",
        )
    elif cmd == "/remove":
        storage.remove_from_user_list(chat_id, symbol, exchange)
        log.info("Removed %s (%s) for chat %s", symbol, exchange, chat_id)
        reply(chat_id, f"Removed {symbol} ({exchange}) if it was present.")
    else:
        reply(chat_id, HELP_TEXT)


def _reply_suggestions(chat_id, query):
    """Reply with matching stocks from the NSE list when an exact symbol fails."""
    matches = sources.search_stocks(query, limit=10)
    if not matches:
        log.info(
            "No stock matched '%s' for chat %s - nothing added", query, chat_id
        )
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
    except Exception as exc:  # broad on purpose: never let a getUpdates hiccup kill the run
        log.warning("getUpdates failed: %s", config.redact(exc), exc_info=True)
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


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(args), capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            list(args), 124, stdout="", stderr=f"command timed out after {timeout}s"
        )


# The four state files that must reach GitHub to survive a redeploy.
STATE_FILES = (
    config.WATCHLIST_FILE,
    config.SUBSCRIPTIONS_FILE,
    config.SETTINGS_FILE,
    config.SEEN_FILE,
)


def pending_state_changes() -> str:
    """Comma-separated names of state files with uncommitted changes.

    Empty string means the worktree is clean. Used by /status and by the
    always-on server's periodic flush to decide whether a push is needed.
    """
    res = _git(
        "git", "status", "--porcelain", "--untracked-files=no",
        *[str(f) for f in STATE_FILES],
    )
    if res.returncode != 0:
        return ""
    names = []
    for line in res.stdout.splitlines():
        path = line[3:].strip().strip('"')
        names.append(Path(path).name)
    return ", ".join(sorted(set(names)))


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
    added = _git("git", "add", *[str(f) for f in STATE_FILES])
    if added.returncode != 0:
        log.warning(
            "git add failed - state NOT pushed (local changes kept): %s",
            added.stderr.strip()[-300:],
        )
        return False
    staged = _git("git", "diff", "--cached", "--name-only").stdout.strip()
    if not staged:
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
    log.info(
        "Staged state files: %s", ", ".join(staged.splitlines())
    )

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


# ------------------------------------------------------------------- diag
def main_check() -> int:
    """Diagnostic for the 'my changes vanish on redeploy' problem.

    Prints whether the GitHub push is configured, whether the token can
    actually read/write the repo, which branch state is pushed to, and
    whether any state is currently unsaved. Exit code 0 = persistence OK.

    Run on the host itself (e.g. Render's Shell tab):
        python run_bot.py --check
    """
    print("=" * 62)
    print("Persistence diagnostic - will /add survive a redeploy?")
    print("=" * 62)

    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    ok = bool(token and repo)

    print("\n[1] Environment")
    print(
        "  GH_TOKEN            : "
        + (f"SET ({token[:4]}...)" if token else "NOT SET")
    )
    print(f"  GITHUB_REPOSITORY   : {repo or 'NOT SET'}")

    sha = _git("git", "rev-parse", "--short", "HEAD").stdout.strip() or "unknown"
    sym = _git("git", "symbolic-ref", "--short", "HEAD").stdout.strip()
    branch = _push_branch("")
    detached = not sym
    print("\n[2] Git")
    print(
        f"  HEAD                : {sha} "
        f"({'detached HEAD' if detached else 'on branch ' + sym})"
    )
    print(f"  Push/sync branch    : {branch}")
    for f in STATE_FILES:
        tracked = _git("git", "ls-files", "--error-unmatch", str(f)).returncode == 0
        status = "tracked" if tracked else "NOT tracked - push_state cannot save it"
        print(f"  {f.name:<22}: {status}")
        if not tracked:
            ok = False
    pending = pending_state_changes()
    print(f"  Uncommitted state   : {pending or 'none'}")

    if token and repo:
        url = f"https://x-access-token:{token}@github.com/{repo}.git"
        print(f"\n[3] GitHub access via GH_TOKEN (repo: {repo})")
        ls = _git("git", "ls-remote", url, "HEAD")
        if ls.returncode == 0:
            print("  read  (ls-remote)    : OK")
        else:
            print(f"  read  (ls-remote)    : FAILED - {ls.stderr.strip()[-200:]}")
            ok = False
        dry = _git("git", "push", "--dry-run", url, "HEAD:refs/heads/__state_check__")
        if dry.returncode == 0:
            print(
                "  write (push dry-run) : OK - a push would be accepted "
                "(no branch created)"
            )
        else:
            print(f"  write (push dry-run) : FAILED - {dry.stderr.strip()[-300:]}")
            ok = False
    else:
        print("\n[3] GitHub access: skipped (set GH_TOKEN and GITHUB_REPOSITORY first)")

    print("\n[4] Verdict")
    if ok:
        print(f"  OK - state is pushed to GitHub ({repo}, branch {branch}) and")
        print("  WILL survive redeploys. Confirm in Telegram with /status.")
    else:
        print("  NOT OK - changes saved here will be LOST on redeploy.")
        print("  Fix the items above, then re-run:  python run_bot.py --check")
        print("  (On Render: set GH_TOKEN + GITHUB_REPOSITORY in the service's")
        print("   environment, redeploy, and run this again from the Shell tab.)")
    print()
    return 0 if ok else 1


# ------------------------------------------------------------------------- main
def main():
    if any(a.lower() == "--check" for a in sys.argv[1:]):
        sys.exit(main_check())
    log.info("Processing Telegram commands...")
    checknow_chat = process_commands()
    log.info("Running poll cycle%s...", f" (forced for {checknow_chat})" if checknow_chat else "")
    sent = poller.run_once(force=bool(checknow_chat), only_chat=checknow_chat)
    log.info("Pushing state if changed...")
    push_state()
    log.info("Done. Sent %s alert(s).", sent)


if __name__ == "__main__":
    main()
