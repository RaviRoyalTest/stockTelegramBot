"""System status command: where your data lives + GitHub push state (/status)."""
from __future__ import annotations

import html

from .. import config, storage
from ..formatting.schedule import format_schedule
from ..github import (
    _push_branch,
    github_push_configured,
    pending_state_changes,
    push_error,
)
from .reply import reply


def handle_status(chat_id) -> None:
    """Render the /status report: role, where your data lives, GitHub push."""
    gh_configured = github_push_configured()
    owner = storage.is_owner(chat_id)
    location = storage.list_location(chat_id)
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
        if push_error:
            push_status += " - last push FAILED"
            sync_line += f" (last error: {push_error})"
    else:
        push_status = (
            "NOT set - your changes stay only on this host's disk (lost "
            "on redeploy). Set GH_TOKEN + GITHUB_REPOSITORY on this host."
        )
        sync_line = "Local state vs GitHub: unknown (no GitHub credentials)"
    personal_line = (
        "<b>Personal:</b> everything here is yours alone - your watchlist, "
        "schedule, settings and alerts. Other users' data never mixes "
        "with yours and they cannot touch yours."
    )
    reply(
        chat_id,
        "\n".join(
            [
                f"<b>Your chat id:</b> <code>{chat_id}</code>",
                f"<b>Role:</b> {'owner' if owner else 'subscriber'}",
                personal_line,
                f"<b>Saved in:</b> <code>{html.escape(location)}</code>",
                f"<b>GitHub push:</b> {html.escape(push_status)}",
                html.escape(sync_line),
                f"<b>Scheduled reports:</b> "
                + ("enabled" if config.SCHEDULED_REPORTS_ENABLED and config.PROCESS_COMMANDS else "off")
                + " \u00b7 " + html.escape(format_schedule(chat_id).split("\n")[0])
                + f" \u00b7 manage with /schedule",
                "Run /watchlist to see your current watchlist.",
            ]
        ),
    )
