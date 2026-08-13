"""Telegram bot package: dispatcher, command families, registry and runner.

Layout (one module per concern - see AGENTS.md):
  dispatch.py      command router (handle_command / handle_query_text / callbacks)
  registry.py      alias table, usage hints, /help and the Telegram menu
  reply.py         send one message or a chunked series
  helpers.py       shared small helpers (quotes, symbol suggestions)
  corporate_action_commands.py  /corpactions, /exdates, /corpactionssummary
  watchlist_commands.py  /watchlist, /addstock, /removestock, favourites, /news
  settings_commands.py  /alertfilters, /pricealert, /watcher, /fundmode
  schedule_commands.py  /menu, /schedule
  movers_commands.py  /movers, /topgainers, /toplosers
  fundamentals_commands.py  /fundamentalanalyze, /fundamentalreport
  harmonic_commands.py  /harmonicpatterns
  scanner_commands.py  /scan500
  status.py        /status
  runner.py        cron-style polling loop + CLI entry (python run_bot.py)
"""
from . import dispatch, registry, reply, runner

__all__ = ["dispatch", "registry", "reply", "runner"]
