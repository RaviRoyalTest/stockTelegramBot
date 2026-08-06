"""Always-on bot: long-polls Telegram and answers commands instantly.

Run it on any always-on host (or locally / in the current environment).
The GitHub Actions cron continues to work 24/7 as a fallback; this process
just makes responses to /add, /remove, /list, /checknow, /help instant.

Render treats this as a Web Service and requires the process to bind to
$PORT (default 10000). This script starts a tiny HTTP health-check server
on that port in a background thread so deployment succeeds; the Telegram
long-polling loop runs in the main thread.

Usage:  python bot_server.py
"""
import http.server
import logging
import os
import socketserver
import subprocess
import sys
import threading
import time

from corp_actions import config
from corp_actions.poller import poller
from run_bot import get_updates, handle_command, push_state, reply, sync_state


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
log = logging.getLogger("bot_server")

# Commands that modify state and therefore need to be pushed back to GitHub.
WRITE_COMMANDS = {"/add", "/remove", "/filter", "/alert"}

# Bind retry for the health server: PaaS instance swaps can briefly leave
# the port held by the previous process, and Render only waits ~90s for the
# first health check before timing the deploy out.
_BIND_ATTEMPTS = 10
_BIND_RETRY_DELAY = 3  # seconds between attempts


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server so a slow health probe never blocks anything."""
    daemon_threads = True


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal handler that lets Render / uptime checks know we're alive."""

    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()

    def log_message(self, format, *args):
        # Keep health probes out of the logs (Render pings this every ~5s).
        return


def start_health_server():
    """Bind an HTTP health endpoint on $PORT (Render default 10000).

    Render's Web Service health check requires the process to listen on
    $PORT; without this, deployments time out even though the Telegram bot
    is running fine. The bind is retried for ~30s to survive transient
    port conflicts during instance swaps. Returns the port bound, or None
    if it could not bind (the bot still runs either way, but the deploy
    will report a timeout).
    """
    try:
        port = int(os.getenv("PORT", "10000"))
    except (TypeError, ValueError):
        log.warning(
            "Invalid PORT env %r - falling back to 10000",
            os.getenv("PORT"),
        )
        port = 10000
    server = None
    last_error = None
    for attempt in range(1, _BIND_ATTEMPTS + 1):
        try:
            server = _ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
            break
        except OSError as exc:
            last_error = exc
            if attempt < _BIND_ATTEMPTS:
                log.warning(
                    "Health server bind attempt %s/%s failed on port %s "
                    "(%s) - retrying...",
                    attempt, _BIND_ATTEMPTS, port, exc,
                )
                time.sleep(_BIND_RETRY_DELAY)
    if server is None:
        log.error(
            "DEPLOYMENT WILL TIMEOUT: could not bind health server on "
            "port %s after %s attempts (%s). Render only marks a Web "
            "Service deploy complete once that port answers HTTP. Free "
            "the port or set a free $PORT, then redeploy.",
            port, _BIND_ATTEMPTS, last_error,
        )
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-http")
    thread.start()
    log.info(
        "Health server listening on http://0.0.0.0:%s/ - Render deploy "
        "health check will pass",
        port,
    )
    return port


def _deployed_commit() -> str:
    """Short SHA of the code this process is running (for log diagnosis)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        ).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def main():
    log.info(
        "Deployed commit %s (health server was added in b7231ef - if this "
        "SHA is older, deploy latest main and the timeout will clear)",
        _deployed_commit(),
    )
    log.info(
        "Owner TELEGRAM_CHAT_ID=%s - /add from the owner updates "
        "watchlist.json; other chats update subscriptions.json",
        config.TELEGRAM_CHAT_ID or "NOT SET",
    )
    if not os.getenv("GH_TOKEN") or not os.getenv("GITHUB_REPOSITORY"):
        log.warning(
            "GH_TOKEN / GITHUB_REPOSITORY not set: watchlist and subscription "
            "changes will NOT be pushed to GitHub and WILL BE LOST on redeploy. "
            "Set both (a fine-grained PAT with Contents:write) in the host's "
            "environment to persist state, then use /status in Telegram to "
            "confirm."
        )
    start_health_server()
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
                log.info("command from chat %s: %s", chat_id, text)
                if cmd == "/checknow":
                    handle_command(chat_id, text)
                    try:
                        sent = poller.run_once(force=True, only_chat=str(chat_id))
                        reply(chat_id, f"Check done - re-sent {sent} alert(s) to this chat.")
                    except Exception as exc:  # keep the loop alive
                        reply(chat_id, f"Check failed: {exc}")
                else:
                    try:
                        handle_command(chat_id, text)
                        # Persist write commands back to GitHub so workflow
                        # re-runs and redeploys never lose what users added.
                        # Read-only commands (/list, /status, /next, /help)
                        # never push or reset - a failed push from a previous
                        # write stays on the disk instead of being wiped by
                        # reset --hard, and /list always reflects the latest
                        # local state.
                        if cmd in WRITE_COMMANDS:
                            try:
                                ok = push_state()
                                if ok:
                                    log.info(
                                        "State pushed to GitHub after %s "
                                        "(chat %s)",
                                        cmd, chat_id,
                                    )
                                    # Pull anything the cron pushed so we
                                    # never overwrite newer commits.
                                    sync_state()
                                else:
                                    log.warning(
                                        "State NOT pushed for %s - change is "
                                        "saved locally but will be LOST on "
                                        "redeploy unless GH_TOKEN/"
                                        "GITHUB_REPOSITORY are set.",
                                        cmd,
                                    )
                                    reply(
                                        chat_id,
                                        "⚠️ Your change was saved only on this "
                                        "server's disk, NOT pushed to GitHub. "
                                        "It will be LOST on the next redeploy. "
                                        "Run /status to check the GitHub push "
                                        "configuration (GH_TOKEN / "
                                        "GITHUB_REPOSITORY must be set on this "
                                        "host).",
                                    )
                            except Exception as exc:
                                log.warning(
                                    "state push failed: %s", config.redact(exc)
                                )
                    except Exception as exc:
                        log.warning(
                            "command %s from chat %s failed: %s",
                            cmd, chat_id, config.redact(exc),
                        )
                        reply(
                            chat_id,
                            "Command failed on the server: "
                            f"{config.redact(exc)}. Use /help.",
                        )
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