"""Always-on bot: long-polls Telegram and answers commands instantly.

Run it on any always-on host (or locally / in the current environment).
The GitHub Actions cron continues to work 24/7 as a fallback; this process
just makes responses to /add, /remove, /list, /checknow, /help instant.

Usage:  python bot_server.py
"""
import logging
import sys
import time

from corp_actions.poller import poller
from run_bot import get_updates, handle_command, reply

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("bot_server")


def main():
    log.info("Starting long-polling bot (instant responses)...")
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
                offset = update["update_id"] + 1
        except Exception as exc:
            log.warning("poll error (retrying): %s", exc)
        time.sleep(1)


if __name__ == "__main__":
    main()
