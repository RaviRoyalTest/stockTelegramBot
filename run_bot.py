"""Entry point for running in a GitHub Actions cron job.

Two jobs, one run:
  1. Optionally process Telegram bot commands (/addstock, /removestock, /watchlist, /help)
     when PROCESS_COMMANDS=true (default). Set PROCESS_COMMANDS=false in the
     GitHub Actions cron so the always-on bot server is the only process that
     polls getUpdates (avoids double replies and 409 conflicts). Any change
     is committed and pushed back to the repo using GH_TOKEN.
  2. Run one poll cycle: fetch corporate actions, filter to the watchlist,
     and send new ones to Telegram.

Local usage:  python run_bot.py

All logic lives in corp_actions.bot (dispatcher, command families, runner);
this file only configures logging and calls the runner.
"""
import logging
import sys

# The --check diagnostic only talks to git / the environment, so it must run
# even when requirements.txt hasn't been installed yet - import it directly
# from the dependency-light github module (no third-party deps).
if any(a.lower() == "--check" for a in sys.argv[1:]):
    from corp_actions.github import main_check

    sys.exit(main_check())

from corp_actions.bot.runner import ImmediateStreamHandler, main

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    handlers=[ImmediateStreamHandler(sys.stdout)],
)

if __name__ == "__main__":
    main()
