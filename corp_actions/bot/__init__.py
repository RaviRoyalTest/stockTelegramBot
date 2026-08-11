"""Telegram bot package: dispatcher, command families, registry and runner.

Layout (one module per concern - see AGENTS.md):
  dispatch.py      command router (handle_command / handle_query_text / callbacks)
  registry.py      alias table, usage hints, /help and the Telegram menu
  reply.py         send one message or a chunked series
  helpers.py       shared small helpers (quotes, symbol suggestions)
  ca_cmds.py       /corpactions, /exdates, /corpactionssummary
  watchlist_cmds.py /watchlist, /addstock, /removestock, favourites, /news
  settings_cmds.py /alertfilters, /pricealert, /watcher, /moversfund
  schedule_cmds.py /menu, /schedule
  movers_cmds.py   /movers, /topgainers, /toplosers
  fund_cmds.py     /fundamentalanalyze, /fundamentalreport
  harmonic_cmds.py /harmonicpatterns
  scan_cmds.py     /scan500
  status.py        /status
  runner.py        cron-style polling loop + CLI entry (python run_bot.py)
"""
from . import dispatch, registry, reply, runner

__all__ = ["dispatch", "registry", "reply", "runner"]
