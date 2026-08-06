"""Always-on bot: long-polls Telegram and answers commands instantly.

Run it on any always-on host (or locally / in the current environment).
The GitHub Actions cron continues to work 24/7 as a fallback; this process
just makes responses to /add, /remove, /list, /checknow, /help instant.

Usage:  python bot_server.py
"""
import logging
import os
import sys
import time

from corp_actions import config
from corp_actions.poller import poller
from run_bot import get_updates, handle_command, push_state, reply, sync_state

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("bot_server")


# Commands that modify state and therefore need to be pushed back to GitHub.
WRITE_COMMANDS = {"/add", "/remove", "/filter", "/alert"}


def main():
    if not os.getenv("GH_TOKEN") or not os.getenv("GITHUB_REPOSITORY"):
        log.warning(
            "GH_TOKEN / GITHUB_REPOSITORY not set: watchlist and subscription "
            "changes will NOT be pushed to GitHub and WILL BE LOST on redeploy. "
            "Set both (a fine-grained PAT with Contents:write) in the host's "
            "environment to persist state, then use /status in Telegram to "
            "confirm."
        )
    log.info("Starting long-polling bot (instant responses)...")
    sync_state()
    offset = None
    while True:
        try:
            updates = get_updates(offset=offset)
            for update in updates:
                message = update.get("message") or {}
                text = (message.get("text") or "").strip()
                chat_id = (message.get("chat") or {}).get("id")
                if not text.startswith("/"):
                    continue
                cmd = text.strip().lower()
                if cmd == "/checknow":
                    handle_command(chat_id, text)
                    try:
                        sent = poller.run_once(force=True, only_chat=str(chat_id))
                        reply(chat_id, f"Check done - re-sent {sent} alert(s) to this chat.")
                    except Exception as exc:  # keep the loop alive
                        reply(chat_id, f"Check failed: {exc}")
                else:
                    handle_command(chat_id, text)
                    # Persist write commands back to GitHub so workflow re-runs
                    # and redeploys never lose what users added. Read-only
                    # commands (/list, /status, /next, /help) never push or
                    # reset - a failed push from a previous write stays on the
                    # disk instead of being wiped by reset --hard, and /list
                    # always reflects the latest local state.
                    if cmd in WRITE_COMMANDS:
                        try:
                            ok = push_state()
                            if ok:
                                # Pull anything the cron pushed so we never
                                # overwrite newer commits.
                                sync_state()
                            else:
                                log.warning(
                                    "State NOT pushed for %s - your change is "
                                    "saved locally but will be LOST on redeploy "
                                    "unless GH_TOKEN/GITHUB_REPOSITORY are set.",
                                    cmd,
                                )
                                reply(
                                    chat_id,
                                    "⚠️ Your change was saved only on this "
                                    "server's disk, NOT pushed to GitHub. It "
                                    "will be LOST on the next redeploy. Run "
                                    "/status to check the GitHub push "
                                    "configuration (GH_TOKEN / "
                                    "GITHUB_REPOSITORY must be set on this "
                                    "host).",
                                )
                        except Exception as exc:
                            log.warning("state push failed: %s", config.redact(exc))
                offset = update["update_id"] + 1
        except Exception as exc:
            err = config.redact(exc)
            if "409" in err:
                log.warning(
                    "409 Conflict: another process is polling this bot token. "
                    "Only ONE process may call getUpdates - stop the other "
                    "bot_server.py / disable command processing in the GitHub "
                    "Actions cron (PROCESS_COMMANDS=false). %s",
                    err,
                )
            else:
                log.warning("poll error (retrying): %s", err)
        time.sleep(1)


if __name__ == "__main__":
    main()
