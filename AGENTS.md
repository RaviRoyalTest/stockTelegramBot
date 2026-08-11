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
  belong in their own modules (`corp_actions/scheduler.py`, etc.).
- Open/Closed: extend behavior by adding new modules/commands, not by
  editing the dispatch core for every new feature.
- Liskov Substitution / Interface Segregation: keep module APIs narrow and
  stable; pass the exact dependencies a function needs (dependency injection
  via parameters) instead of importing the giant `run_bot` module everywhere.
- Dependency Inversion: modules in `corp_actions/` must not import
  `run_bot` (it would create circular imports). Pass callbacks (e.g. the
  command runner) in as parameters instead.

### Folder structure

```
run_bot.py              entry point for the GitHub Actions cron (thin)
bot_server.py           always-on long-polling server entry (thin)
app.py / dashboard.py / dashboard_server.py   Streamlit UI / combined server
corp_actions/           the actual logic, split by responsibility:
    config.py           env config / defaults
    sources.py          NSE/BSE/Yahoo fetch + parsing
    storage.py          JSON state files + locking
    notifier.py         Telegram send / formatting
    poller.py           background poll cycle
    scheduler.py        scheduled-reports loop (owns its own timing state)
    harmonic.py         harmonic-pattern analysis
```

## 3. Other conventions

- Python 3 only. Use `python3` (there is no `python` on most hosts).
- Never commit `*.lock` files (gitignored).
- Commit and push to `main` after each task (see project instructions).
- `python3 -m py_compile` the files you touch before committing.
