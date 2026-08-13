"""Detailed command guide: /learn [TOPIC | /COMMAND | all].

The bot has 30+ commands, so /learn explains every one of them in a way
that is easy to recollect: what it does, the syntax, copy-paste examples,
what the output means, and tips. A bare /learn shows the topic index;
`/learn TOPIC` walks through a group (e.g. 'stocks', 'schedule', 'alerts');
`/learn /COMMAND` gives the full walkthrough of one command; `/learn all`
prints the entire guide. All rendering logic lives in the pure module
`bot/learn_texts.py`; this handler only routes the argument and replies.
"""
from __future__ import annotations

import logging

from ..core.text import escape, split_messages
from .learn_texts import (
    learn_all_lines,
    learn_command_lines,
    learn_index_lines,
    learn_topic_lines,
    resolve_target,
)
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_USAGE = (
    "Usage: <code>/learn [TOPIC | /COMMAND | all]</code>\n"
    "<code>/learn</code>           \u2192 the topic index\n"
    "<code>/learn stocks</code>    \u2192 fundamental analysis group\n"
    "<code>/learn schedule</code>  \u2192 automation group\n"
    "<code>/learn /scan500</code>  \u2192 full walkthrough of one command\n"
    "<code>/learn all</code>       \u2192 the entire guide\n"
    "Aliases: <code>/guide</code>, <code>/explain</code>, <code>/tutorial</code>, "
    "<code>/howto</code>"
)


def handle_learn(chat_id, parts) -> None:
    """Walk the user through every command, one topic or command at a time."""
    if len(parts) < 2:
        reply(chat_id, escape("\n".join(learn_index_lines())))
        return

    arg = " ".join(parts[1:]).strip()
    if arg.strip("/").lower() == "all":
        reply_messages(chat_id, split_messages(learn_all_lines()))
        return

    target = resolve_target(arg)
    if target is None:
        reply(
            chat_id,
            f"\U0001F6AB I don't have a guide entry for <code>{escape(arg)}</code>.\n"
            + escape("\n".join(learn_index_lines())),
        )
        return

    kind, name = target
    if kind == "command":
        lines = learn_command_lines(name)
    else:
        lines = learn_topic_lines(name)

    reply_messages(chat_id, split_messages(lines))
