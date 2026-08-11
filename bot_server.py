"""Always-on bot: long-polls Telegram and answers commands instantly.

Run it on any always-on host (or locally / in the current environment).
The GitHub Actions cron continues to work 24/7 as a fallback; this process
just makes responses to /addstock, /removestock, /watchlist, /checknow, /help instant.

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

from corporate_actions import config
from corporate_actions.bot.dispatch import handle_callback_query, handle_command, handle_query_text
from corporate_actions.bot.registry import register_commands
from corporate_actions.bot.reply import reply
from corporate_actions.bot.runner import start_scheduled_reports
from corporate_actions.github import (
    _ahead_of_origin,
    _push_branch,
    github_push_configured,
    pending_state_changes,
    push_error,
    push_state,
    sync_state,
)
from corporate_actions.poller import poller
from corporate_actions.telegram.client import get_updates


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
# New descriptive names plus the old short aliases all count as writes.
WRITE_COMMANDS = {
    "/add", "/addstock", "/remove", "/removestock",
    "/filter", "/alertfilters", "/actionfilters", "/alert", "/pricealert",
    "/sched", "/schedule",
    # /watcher writes the user's watcher settings (on/off, threshold,
    # universe) to settings.json - without an immediate push, a redeploy
    # inside the 180s flush window would silently reset it to OFF.
    "/watcher", "/bigmover", "/moverwatch",
}

# How often to retry pushing state that did not reach GitHub (seconds). A
# failed push is retried automatically so a transient GitHub hiccup never
# leaves data stuck on the ephemeral disk until the next command - that is
# exactly how a stock can 'vanish' on the next redeploy.
PUSH_FLUSH_SECONDS = int(os.getenv("PUSH_FLUSH_SECONDS", "180"))

# Backoff (seconds) after a Telegram getUpdates 409 conflict. During a Render
# deploy the old and new instances briefly overlap and both poll getUpdates;
# the conflict clears when the old one shuts down. Sleeping here instead of
# polling every second keeps the log quiet and stops the loop from fighting
# the other process for the whole deploy window.
_CONFLICT_BACKOFF = float(os.getenv("GETUPDATES_CONFLICT_BACKOFF", "20"))

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
        except OSError as error:
            last_error = error
            if attempt < _BIND_ATTEMPTS:
                log.warning(
                    "Health server bind attempt %s/%s failed on port %s "
                    "(%s) - retrying...",
                    attempt, _BIND_ATTEMPTS, port, error,
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


def flush_pending_state() -> None:
    """Push state that was written but never made it to GitHub.

    Catches two silent data-loss cases:
      * uncommitted changes left on the ephemeral disk (e.g. a previous
        push failed and was rolled back), and
      * local commits that were never pushed - the worktree looks clean,
        but the branch is ahead of origin, so the data is still wiped on
        redeploy.
    """
    if not github_push_configured():
        # No credentials - there is nothing to retry, and push_state already
        # logged the missing-GH_TOKEN warning once. Skipping avoids log spam
        # every cycle while the problem is unconfigured.
        return
    branch = _push_branch("")
    pending = pending_state_changes()
    ahead = _ahead_of_origin(branch)
    if not pending and not ahead:
        return
    log.info(
        "Flushing state to GitHub (branch %s): %s%s",
        branch,
        pending or "no uncommitted changes",
        " | unpushed local commits" if ahead else "",
    )
    try:
        if push_state():
            log.info("State flushed to GitHub")
            sync_state()
        else:
            log.warning(
                "Flush push failed - will retry in %ss", PUSH_FLUSH_SECONDS
            )
    except Exception as error:
        log.warning("Flush push failed: %s", config.redact(error))


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
    # When SERVE_DASHBOARD is enabled (default), run the Streamlit dashboard
    # as the main process and the Telegram bot in a background thread. This
    # means the existing Render start command (`python bot_server.py`) serves
    # the web dashboard with no extra configuration.
    serve_dashboard = os.getenv("SERVE_DASHBOARD", "true").strip().lower() in ("1", "true", "yes", "on")
    if serve_dashboard:
        log.info("SERVE_DASHBOARD enabled - starting dashboard + bot together")
        try:
            import dashboard_server
            dashboard_server.main()
            return
        except ImportError:
            log.warning(
                "dashboard_server not importable - falling back to bot-only mode"
            )

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
    # When running inside dashboard_server (SERVE_DASHBOARD=false), the
    # Streamlit process binds $PORT, so skip the separate health server to
    # avoid a port conflict.
    if serve_dashboard or os.getenv("SERVE_DASHBOARD", "true").strip().lower() not in ("0", "false", "off", "no"):
        start_health_server()
    register_commands()
    start_scheduled_reports()
    # The always-on server runs the background poller (corporate-action alerts,
    # ex-date reminders, price alerts AND the sudden-move watcher). The GitHub
    # Actions cron runs with PROCESS_COMMANDS=false so it never double-polls.
    if config.PROCESS_COMMANDS:
        poller.start()
        log.info("Background poller + sudden-move watcher started")
    log.info("Starting long-polling bot (instant responses)...")
    sync_state()
    # Push anything a previous run left behind (failed push, crash before
    # push) before serving commands.
    flush_pending_state()
    last_flush = time.monotonic()
    offset = None
    while True:
        try:
            updates = get_updates(offset=offset)
            for update in updates:
                message = update.get("message") or {}
                text = (message.get("text") or "").strip()
                chat_id = (message.get("chat") or {}).get("id")
                callback = update.get("callback_query")
                if callback:
                    try:
                        handle_callback_query(callback)
                    except Exception as error:
                        log.warning("callback query failed: %s", config.redact(error))
                    offset = update["update_id"] + 1
                    continue
                if not text.startswith("/"):
                    try:
                        handle_query_text(chat_id, text)
                    except Exception as error:
                        log.warning("natural query failed: %s", config.redact(error))
                    offset = update["update_id"] + 1
                    continue
                # The bare command name only (e.g. "/addstock" for "/addstock HDFCBANK")
                # so write-command detection below actually matches. Comparing
                # the full text - as before - meant "/addstock hdfcbank" never
                # equalled "/addstock", so the immediate GitHub push after a write
                # command never happened and state only reached GitHub via the
                # periodic flush (or was lost on redeploy).
                parts = text.strip().split()
                command = parts[0].lower().split("@")[0] if parts else ""
                log.info("command from chat %s: %s", chat_id, text)
                if command == "/checknow":
                    handle_command(chat_id, text)
                    try:
                        sent = poller.run_once(force=True, only_chat=str(chat_id))
                        reply(chat_id, f"Check done - re-sent {sent} alert(s) to this chat.")
                    except Exception as error:  # keep the loop alive
                        reply(chat_id, f"Check failed: {config.redact(error)}")
                else:
                    try:
                        handle_command(chat_id, text)
                        # Persist write commands back to GitHub so workflow
                        # re-runs and redeploys never lose what users added.
                        # Read-only commands (/watchlist, /status, /corpactionsformylist, /help)
                        # never push or reset - a failed push from a previous
                        # write stays on the disk instead of being wiped by
                        # reset --hard, and /watchlist always reflects the latest
                        # local state.
                        if command in WRITE_COMMANDS:
                            try:
                                success = push_state()
                                if success:
                                    log.info(
                                        "State pushed to GitHub after %s "
                                        "(chat %s)",
                                        command, chat_id,
                                    )
                                    # Pull anything the cron pushed so we
                                    # never overwrite newer commits.
                                    sync_state()
                                else:
                                    log.warning(
                                        "State NOT pushed for %s - change is "
                                        "saved locally but will be LOST on "
                                        "redeploy: %s",
                                        command, push_error,
                                    )
                                    reply(
                                        chat_id,
                                        "⚠️ Your change was saved only on this "
                                        "server's disk, NOT pushed to GitHub. "
                                        "It will be LOST on the next redeploy. "
                                        f"Reason: {push_error}. Run "
                                        "/status for details, or `python "
                                        "run_bot.py --check` on the host.",
                                    )
                            except Exception as error:
                                log.warning(
                                    "state push failed: %s", config.redact(error)
                                )
                    except Exception as error:
                        log.warning(
                            "command %s from chat %s failed: %s",
                            command, chat_id, config.redact(error),
                        )
                        reply(
                            chat_id,
                            "Command failed on the server: "
                            f"{config.redact(error)}. Use /help.",
                        )
                offset = update["update_id"] + 1
            now = time.monotonic()
            if now - last_flush >= PUSH_FLUSH_SECONDS:
                last_flush = now
                flush_pending_state()
        except Exception as error:
            error = config.redact(error)
            if "409" in error:
                # Another process currently holds getUpdates for this token -
                # normally the OLD Render instance still shutting down during
                # a deploy (both call getUpdates until the old one dies). Back
                # off instead of hammering every second; the conflict clears
                # on its own once the other process exits.
                log.warning(
                    "409 Conflict: another process is polling this bot token. "
                    "This usually clears on its own when the old deploy "
                    "instance shuts down - backing off %.1fs. If it persists, "
                    "stop the other bot_server.py / ensure the GitHub Actions "
                    "cron keeps PROCESS_COMMANDS=false. %s",
                    _CONFLICT_BACKOFF, error,
                )
                time.sleep(_CONFLICT_BACKOFF)
            else:
                log.warning("poll error (retrying): %s", error)
        time.sleep(1)


if __name__ == "__main__":
    main()