"""GitHub state sync: commit + push / pull the JSON state files.

The JSON state files (watchlist, subscriptions, settings, seen, schedule) are
the source of truth. The always-on server and the GitHub Actions cron both
push them back to the repo so they survive ephemeral disks (Render) and
re-runs. This module is the only place that talks to git.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


def github_push_configured() -> bool:
    """True only when the host can actually push state back to GitHub."""
    return bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))


# The five state files that must reach GitHub to survive a redeploy.
STATE_FILES = (
    config.WATCHLIST_FILE,
    config.SUBSCRIPTIONS_FILE,
    config.SETTINGS_FILE,
    config.SEEN_FILE,
    config.SCHEDULE_FILE,
)


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(args), capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            list(args), 124, stdout="", stderr=f"command timed out after {timeout}s"
        )


def _remote_default_branch(remote_url) -> str:
    try:
        output = _git("git", "ls-remote", "--symref", remote_url, "HEAD").stdout
        for line in output.splitlines():
            if line.strip().startswith("ref:"):
                # Line looks like:  ref: refs/heads/main\tHEAD
                # The ref path is the SECOND token; the trailing "HEAD" is
                # only the name of the ref being described. Taking the LAST
                # token here returns "HEAD", which turns the push refspec
                # into HEAD:HEAD and fails every push from a detached
                # checkout (e.g. Render) with "You must fully qualify the
                # ref" - the exact bug that made stocks vanish on redeploy.
                return line.split()[1].removeprefix("refs/heads/")
    except Exception:
        pass
    if remote_url:
        log.warning(
            "Could not determine the remote default branch (git ls-remote "
            "failed) - state will be pushed to 'main'. If the repo's "
            "default branch is not 'main', set GH_PUSH_BRANCH to override."
        )
    return ""


def _push_branch(remote_url: str) -> str:
    """Resolve the branch that state is pushed to / synced from."""
    branch = os.getenv("GH_PUSH_BRANCH") or ""
    if not branch:
        branch = _git("git", "symbolic-ref", "--short", "HEAD").stdout.strip()
    if not branch:
        branch = _remote_default_branch(remote_url)
    if branch == "HEAD":
        # "HEAD" can never be a real branch name - it means resolution
        # leaked/parsed incorrectly. Falling back to main keeps the push
        # refspec valid instead of pushing HEAD:HEAD and failing.
        log.warning("Push branch resolved as 'HEAD' - falling back to main")
        branch = "main"
    return branch or "main"


def pending_state_changes() -> str:
    """Comma-separated names of state files with uncommitted changes.

    Empty string means the worktree is clean. Used by /status and by the
    always-on server's periodic flush to decide whether a push is needed.
    """
    result = _git(
        "git", "status", "--porcelain", "--untracked-files=no",
        *[str(state_file) for state_file in STATE_FILES],
    )
    if result.returncode != 0:
        return ""
    names = []
    for line in result.stdout.splitlines():
        path = line[3:].strip().strip('"')
        names.append(Path(path).name)
    return ", ".join(sorted(set(names)))


def _ahead_of_origin(branch: str) -> bool:
    """True when the local branch has commits not present on origin/{branch}.

    This is the signal that a previous commit was never pushed - pushing
    again is required; a hard reset at this point would destroy data.
    """
    result = _git("git", "rev-list", "--count", f"origin/{branch}..HEAD")
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


# Reason for the last push_state() failure ("" when OK). bot_server reads this
# so the "NOT pushed to GitHub" warning can say WHY instead of guessing.
push_error = ""


def _redact_gh(text) -> str:
    """Mask the GH_TOKEN from git output before it reaches logs or Telegram.

    A failed push echoes the remote URL - including the embedded
    x-access-token - back on stderr. Without masking, the token would leak
    into server logs and into the /status "last error" reply.
    """
    sanitized = str(text)
    token = os.getenv("GH_TOKEN")
    if token:
        sanitized = sanitized.replace(token, "***")
    return sanitized


def push_state() -> bool:
    """Commit and push watchlist/seen state back to the repo, if changed.

    Returns True when the repo is in sync (pushed, or nothing to push).
    Returns False when credentials are missing or the push failed - callers
    should NOT discard local state in that case.

    Handles the expected race with the hourly cron (both push to the same
    branch): on a rejected push it fetches, rebases onto the remote and
    retries once.

    On failure, sets the module-global `push_error` to a short reason.
    """
    global push_error
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        push_error = "GH_TOKEN / GITHUB_REPOSITORY not set on this host"
        log.warning(
            "GH_TOKEN/GITHUB_REPOSITORY not set - skipping push. State is "
            "only on this host's disk and WILL BE LOST on redeploy. Set "
            "GH_TOKEN (fine-grained PAT, Contents: Read and write) and "
            "GITHUB_REPOSITORY (e.g. RaviRoyalTest/stockTelegramBot) in the "
            "host environment."
        )
        return False
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    branch = _push_branch(remote_url)

    _git("git", "config", "user.email", "actions@github.com")
    _git("git", "config", "user.name", "github-actions")
    # Only stage state files that actually exist on disk. A brand-new state
    # file (e.g. schedule.json before the first /sched write) is not in a
    # fresh checkout; "git add" with a nonexistent pathspec aborts with
    # "pathspec did not match any files" and would fail the ENTIRE push,
    # leaving every state change stranded on the ephemeral disk.
    existing = [str(state_file) for state_file in STATE_FILES if state_file.exists()]
    missing = [state_file.name for state_file in STATE_FILES if not state_file.exists()]
    if missing:
        log.info(
            "Skipping git add for missing state file(s): %s",
            ", ".join(sorted(missing)),
        )
    if not existing:
        log.info("No state files on disk - nothing to stage")
        staged = ""
    else:
        add_result = _git("git", "add", *existing)
        if add_result.returncode != 0:
            push_error = "git add failed: " + (_redact_gh(add_result.stderr.strip()[-200:]) or "unknown error")
            log.warning(
                "git add failed - state NOT pushed (local changes kept): %s",
                _redact_gh(add_result.stderr.strip()[-300:]),
            )
            return False
        staged = _git("git", "diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        # Nothing staged. But there may be local commits from a previous run
        # that failed to push. If we are ahead of origin, retry the push
        # instead of claiming "in sync" - otherwise a later sync_state()'s
        # reset --hard would silently destroy those commits.
        if _ahead_of_origin(branch):
            push_result = _git("git", "push", remote_url, f"HEAD:{branch}")
            if push_result.returncode == 0:
                log.info("Pushed previously-unpushed state to %s", branch)
                push_error = ""
                return True
            push_error = "git push failed: " + (_redact_gh(push_result.stderr.strip()[-200:]) or "unknown error")
            log.warning(
                "Retry push of existing local commits failed: %s",
                _redact_gh(push_result.stderr.strip()[-300:]),
            )
            return False
        log.info("No state change to push")
        push_error = ""
        return True
    log.info(
        "Staged state files: %s", ", ".join(staged.splitlines())
    )

    commit_result = _git("git", "commit", "-m", "chore: update watchlist from Telegram")
    if commit_result.returncode != 0:
        # Keep the changes in the worktree instead of the index so a later
        # sync (reset --hard) refuses to wipe them.
        push_error = "git commit failed: " + (_redact_gh(commit_result.stderr.strip()[-200:]) or "unknown error")
        log.warning("State commit failed: %s", _redact_gh(commit_result.stderr.strip()[-300:]))
        _git("git", "reset")
        return False

    push_result = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push_result.returncode == 0:
        log.info("Pushed state to %s", branch)
        push_error = ""
        return True

    # Expected race with the cron: retry once after rebasing onto remote.
    _git("git", "fetch", "origin")
    rebase_result = _git("git", "rebase", f"origin/{branch}")
    if rebase_result.returncode != 0:
        _git("git", "rebase", "--abort")
        push_error = (
            "git push failed after rebase conflict: "
            + (_redact_gh(push_result.stderr.strip()[-200:]) or "unknown error")
        )
        log.warning(
            "Push failed and rebase aborted (conflict): %s",
            _redact_gh(push_result.stderr.strip()[-300:]),
        )
        return False
    retry_push_result = _git("git", "push", remote_url, f"HEAD:{branch}")
    if retry_push_result.returncode == 0:
        log.info("Pushed state to %s (after rebase)", branch)
        push_error = ""
        return True
    push_error = (
        "git push failed after rebase: "
        + (_redact_gh(retry_push_result.stderr.strip()[-200:]) or "unknown error")
    )
    log.warning("Push failed after rebase: %s", _redact_gh(retry_push_result.stderr.strip()[-500:]))
    return False


def sync_state() -> bool:
    """Pull the latest committed state from GitHub before handling commands.

    GitHub is the source of truth; an always-on server's local copy is just a
    working checkout whose disk is ephemeral. Sync before serving commands so
    the server never answers with stale data or overwrites newer state.
    Never resets when the working tree has uncommitted changes (a failed push
    from a previous run) - that would wipe data.
    Returns True when synced or skipped safely (no credentials / dirty tree).
    """
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        log.info("GH_TOKEN/GITHUB_REPOSITORY not set - skipping state sync")
        return True
    try:
        remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        branch = _push_branch(remote_url)
        dirty = _git("git", "status", "--porcelain").stdout.strip()
        if dirty:
            log.warning(
                "State sync skipped: uncommitted changes present - push them "
                "first (dirty: %s)",
                dirty[:200],
            )
            return True
        _git("git", "fetch", "origin")
        if _ahead_of_origin(branch):
            # Local commits exist that were never pushed. A hard reset here
            # would silently destroy them - push them first instead.
            log.warning(
                "State sync skipped: local branch is ahead of origin/%s "
                "(unpushed commits). Run push_state or fix credentials first.",
                branch,
            )
            return True
        result = _git("git", "reset", "--hard", f"origin/{branch}")
        if result.returncode == 0:
            log.info("State synced from origin/%s", branch)
            return True
        log.warning("State sync failed: %s", _redact_gh(result.stderr.strip()[-300:]))
        return False
    except Exception as error:
        log.warning("State sync failed: %s", _redact_gh(error))
        return False


def main_check() -> int:
    """Diagnostic for the 'my changes vanish on redeploy' problem.

    Prints whether the GitHub push is configured, whether the token can
    actually read/write the repo, which branch state is pushed to, and
    whether any state is currently unsaved. Exit code 0 = persistence OK.

    Run on the host itself (e.g. Render's Shell tab):
        python run_bot.py --check
    """
    print("=" * 62)
    print("Persistence diagnostic - will /add survive a redeploy?")
    print("=" * 62)

    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    success = bool(token and repo)

    print("\n[1] Environment")
    print(
        "  GH_TOKEN            : "
        + (f"SET ({token[:4]}...)" if token else "NOT SET")
    )
    print(f"  GITHUB_REPOSITORY   : {repo or 'NOT SET'}")

    commit_sha = _git("git", "rev-parse", "--short", "HEAD").stdout.strip() or "unknown"
    symbol = _git("git", "symbolic-ref", "--short", "HEAD").stdout.strip()
    branch = _push_branch("")
    detached = not symbol
    print("\n[2] Git")
    print(
        f"  HEAD                : {commit_sha} "
        f"({'detached HEAD' if detached else 'on branch ' + symbol})"
    )
    print(f"  Push/sync branch    : {branch}")
    for state_file in STATE_FILES:
        tracked = _git("git", "ls-files", "--error-unmatch", str(state_file)).returncode == 0
        status = "tracked" if tracked else "NOT tracked - push_state cannot save it"
        print(f"  {state_file.name:<22}: {status}")
        if not tracked:
            success = False
    pending = pending_state_changes()
    print(f"  Uncommitted state   : {pending or 'none'}")

    if token and repo:
        url = f"https://x-access-token:{token}@github.com/{repo}.git"
        print(f"\n[3] GitHub access via GH_TOKEN (repo: {repo})")
        ls_remote_result = _git("git", "ls-remote", url, "HEAD")
        if ls_remote_result.returncode == 0:
            print("  read  (ls-remote)    : OK")
        else:
            print(f"  read  (ls-remote)    : FAILED - {_redact_gh(ls_remote_result.stderr.strip()[-200:])}")
            success = False
        dry_run_result = _git("git", "push", "--dry-run", url, "HEAD:refs/heads/__state_check__")
        if dry_run_result.returncode == 0:
            print(
                "  write (push dry-run) : OK - a push would be accepted "
                "(no branch created)"
            )
        else:
            print(f"  write (push dry-run) : FAILED - {_redact_gh(dry_run_result.stderr.strip()[-300:])}")
            success = False
    else:
        print("\n[3] GitHub access: skipped (set GH_TOKEN and GITHUB_REPOSITORY first)")

    print("\n[4] Verdict")
    if success:
        print(f"  OK - state is pushed to GitHub ({repo}, branch {branch}) and")
        print("  WILL survive redeploys. Confirm in Telegram with /status.")
    else:
        print("  NOT OK - changes saved here will be LOST on redeploy.")
        print("  Fix the items above, then re-run:  python run_bot.py --check")
        print("  (On Render: set GH_TOKEN + GITHUB_REPOSITORY in the service's")
        print("   environment, redeploy, and run this again from the Shell tab.)")
    print()
    return 0 if success else 1
