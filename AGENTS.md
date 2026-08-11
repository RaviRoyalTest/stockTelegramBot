# AGENTS.md — Project Rules

Rules for any AI agent or human working on this repository.

## 1. Always pull before starting any work

- Run `git pull --ff-only` (or `git fetch && git merge --ff-only origin/main`)
  before you start a task and before you commit/push, so you never build on a
  stale base or overwrite commits pushed by the deployed bot / GitHub Actions.
- This repo is stateful: `watchlist.json`, `schedule.json`, `settings.json`,
  `seen_actions.json`, `subscriptions.json` are committed and written by the
  always-on bot server AND the hourly GitHub Actions cron. Always pull first.

## 2. Use SOLID principles and keep a proper folder structure

- Single Responsibility: one module = one job. Do not grow `run_bot.py`
  further — command handling, scheduling, and GitHub state-push concerns
  belong in their own modules (`corporate_actions/scheduler.py`, etc.).
- Open/Closed: extend behavior by adding new modules/commands, not by
  editing the dispatch core for every new feature.
- Liskov Substitution / Interface Segregation: keep module APIs narrow and
  stable; pass the exact dependencies a function needs (dependency injection
  via parameters) instead of importing the giant `run_bot` module everywhere.
- Dependency Inversion: modules in `corporate_actions/` must not import
  `run_bot` (it would create circular imports). Pass callbacks (e.g. the
  command runner) in as parameters instead.

### Folder structure

All entry points are thin wrappers - the logic lives in `corporate_actions/`,
organised as small packages of single-purpose modules:

```
run_bot.py                    cron entry: commands + one poll cycle (thin)
bot_server.py                 always-on long-polling server entry (thin)
dashboard.py                  Streamlit dashboard entry (thin)
dashboard_server.py           Render entry: dashboard + bot together
app.py                        Streamlit watchlist editor (thin)
corporate_actions/
  config.py                   env + endpoint configuration (no deps)
  github.py                   git state sync: push/pull JSON state files + --check diag
  scheduler.py                scheduled-reports loop (per-user /schedule)
  core/                       pure primitives: dates.py, numbers.py, text.py
  sources/                    one module per data source (nse, bse, quotes,
                              news, universe, ohlc, screener, fundamentals,
                              rights, types, http, errors) + package facade
  storage/                    one module per state file (watchlist,
                              subscriptions, settings, seen, schedule) over
                              json_file.py atomic base + package facade
  telegram/                   protocol layer: client.py (send/getUpdates),
                              markup.py (keyboard builders)
  formatting/                 message renderers: actions, news, stock,
                              schedule + package facade
  market/                     movement-screen helpers shared by bot + dashboard
                              (periods.py, change.py)
  poller/                     polling engine: engine.py (Poller), events.py,
                              fetchers.py, watcher.py
  scanner/                    NIFTY 500 scanner: indicators, rules, scan,
                              scoring, regime, report
  harmonic/                   harmonic patterns: patterns, analysis, report
  bot/                        Telegram bot package: dispatch.py (router),
                              registry.py (aliases + menu), reply.py, helpers.py,
                              runner.py (cron loop + CLI), and one module per
                              command family (corporate_action_commands, watchlist_commands,
                              settings_commands, schedule_commands, movers_commands,
                              fundamentals_commands, harmonic_commands, scanner_commands, status)
  dashboard_interface/               web dashboard: helpers.py (pure), widgets.py (st),
                              help_text.py, tabs/ (one module per tab), app.py
```

## 3. Other conventions

- Python 3 only. Use `python3` (there is no `python` on most hosts).
- Never commit `*.lock` files (gitignored).
- Commit and push to `main` after each task (see project instructions).
- `python3 -m py_compile` the files you touch before committing.
