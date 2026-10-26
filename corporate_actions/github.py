"""GitHub state sync: commit + push / pull the JSON state files.

The JSON state files (watchlist, subscriptions, settings, seen, schedule) are
the source of truth. The always-on server and the GitHub Actions cron both
push them back to the repo so they survive ephemeral disks (Render) and
re-runs. This module is the only place that talks to git.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from . import config
from .storage.json_file import state_lock
from .state_merge import MISSING, StateConflict, merge_state, strict_json_loads

log = logging.getLogger(__name__)

# Serialize every state git operation. The always-on server runs git from
# MULTIPLE threads: the main command loop (post-command push + sync), the
# periodic flush, and the poller's post-alert push callback. Two overlapping
# git commit/push/rebase/reset --hard runs on the same worktree corrupt each
# other - index.lock collisions, a reset --hard wiping a just-made commit
# (user's removal "comes back" after redeploy), and a failed push's error
# being overwritten to "" by a concurrent success. One reentrant lock makes
# every read-modify-push cycle atomic; RLock because push_state internally
# calls helpers and flush runs push+sync back-to-back on the same thread.
_state_git_lock = threading.RLock()


def github_push_configured() -> bool:
    """True only when the host can actually push state back to GitHub."""
    return bool(os.getenv("GH_TOKEN") and os.getenv("GITHUB_REPOSITORY"))


# The state files that must reach GitHub to survive a redeploy.
# OPENREPORT_DIR is a directory (one file per session date) - git status /
# add accept directory pathspecs, and the missing-skip below tolerates a
# host that has not recorded anything yet. SNAPSHOT_DIR (derived from
# SNAPSHOT_FILE) archives one record per session per market the same way,
# so recorded sessions stay reusable across redeploys.
STATE_FILES = (
    config.WATCHLIST_FILE,
    config.SUBSCRIPTIONS_FILE,
    config.SETTINGS_FILE,
    config.SEEN_FILE,
    config.SCHEDULE_FILE,
    config.SNAPSHOT_FILE,
    config.SNAPSHOT_FILE.parent / "snapshots",
    config.OPENREPORT_DIR,
    # Disposable but valuable: last-known US market caps. Without it a fresh
    # host that cannot fetch a Yahoo crumb blanks the whole US block (0/40).
    config.US_CAPS_FILE,
)


# Supply identity on every commit-producing command, including reconciliation.
_BOT_IDENTITY = ("-c", "user.email=actions@github.com", "-c", "user.name=github-actions")


def _git(*args: str, timeout: int = 30, input_text=None, extra_env=None) -> subprocess.CompletedProcess:
    """Run one git command; returns a CompletedProcess (never raises).

    Pins HTTP/1.1 for all HTTP transports: git's default HTTP/2 stalls
    indefinitely on some networks/hosts (observed on Render and locally) -
    the POST phase hangs forever, so every state push silently fails and
    alerts re-fire after every redeploy (fresh container = stale
    seen_actions.json). Set GIT_HTTP_VERSION in the environment to override.
    """
    command = list(args[1:] if args and args[0] == "git" else args)
    http_version = os.getenv("GIT_HTTP_VERSION", "HTTP/1.1").strip()
    prefix = ["git", "--no-pager"]
    if http_version and command and command[0] in ("push", "fetch", "ls-remote", "pull"):
        prefix += ["-c", f"http.version={http_version}"]
    command = prefix + command
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true",
               GIT_SEQUENCE_EDITOR="true", GIT_OPTIONAL_LOCKS="0")
    if extra_env:
        env.update(extra_env)
    try:
        return subprocess.run(
            command, cwd=config.BASE_DIR, env=env, input=input_text,
            capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            command, 124, stdout="", stderr=f"command timed out after {timeout}s"
        )
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(error))


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
    except Exception as error:
        # Deliberately NOT logged at debug level: the exception text embeds
        # the authenticated remote URL (GH_TOKEN). A downstream warning
        # already explains the fallback to 'main' without any secret.
        log.debug("ls-remote failed (details redacted): %s", type(error).__name__)
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


def _state_paths() -> list[str]:
    """Root-relative pathspecs, including missing (deleted) state files."""
    root = config.BASE_DIR.resolve()
    paths = []
    for path in STATE_FILES:
        path = Path(path)
        absolute = (path if path.is_absolute() else root / path).resolve()
        try:
            paths.append(absolute.relative_to(root).as_posix())
        except ValueError as error:
            raise ValueError(f"State path outside repository: {path}") from error
    return paths


def _failure(label: str, detail: str) -> bool:
    global push_error
    push_error = label + ": " + (_tail(detail, 300) or "unknown error")
    log.warning("%s (local state kept)", push_error)
    return False


def _fetch_state(remote_url: str, branch: str):
    # Fetch the same authenticated remote/branch we push, not an unchecked
    # origin URL which can have different credentials or a stale refspec.
    return _git("git", "fetch", remote_url,
                f"refs/heads/{branch}:refs/remotes/origin/{branch}")


def pending_state_changes() -> str:
    """Comma-separated repo-relative paths of state files with changes.

    Empty string means the worktree is clean. Used by /status and by the
    always-on server's periodic flush to decide whether a push is needed.
    Paths stay repo-relative (data/settings.json) so the label is accurate
    after the state-file reorganization - never bare basenames.
    """
    with state_lock(), _state_git_lock:
        # Status accepts missing pathspecs, so include tracked deletions as
        # well as new/untracked files. Do not confuse a failed status with clean.
        result = _git(
            "git", "status", "--porcelain", "--untracked-files=all",
            "--", *_state_paths(),
        )
        if result.returncode != 0:
            raise RuntimeError("git status failed: " + _tail(result.stderr, 200))
        names = []
        for line in result.stdout.splitlines():
            path = line[3:].strip().strip('"')
            candidate = Path(path)
            if candidate.is_absolute():
                # git prints repo-relative paths when run from the repo root;
                # absolutize-then-relativize only for absolute output.
                try:
                    candidate = candidate.relative_to(config.BASE_DIR)
                except ValueError:
                    candidate = Path(candidate.name)
            names.append(str(candidate).replace("\\", "/"))
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


# Reason for the last push_state() failure ("" when OK). Read via
# last_push_error() so callers ALWAYS see the live value - importing the
# variable directly copies the value at import time (usually ""), which is
# why the Telegram warning once printed an empty "Reason:" and /status
# never showed the real git error.
push_error = ""


def last_push_error() -> str:
    """The live reason for the last push_state() failure ('' when OK)."""
    return push_error


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


def _tail(text, limit: int) -> str:
    """Last `limit` chars of git stderr, cut at a word boundary.

    A raw [-200:] slice can amputate the first word ("remote:" -> "emote:"),
    which is exactly what /status showed. Starting after the first
    whitespace/newline past the cut keeps the message readable.
    """
    cleaned = _redact_gh(text).strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[-limit:]
    for sep in ("\n", " "):
        idx = cut.find(sep)
        if idx != -1:
            return cut[idx + 1 :].strip() or cut.strip()
    return cut.strip()


def _checked(*args, **kwargs) -> str:
    result = _git("git", *args, **kwargs)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed: " + _tail(result.stderr, 300))
    return result.stdout.strip()


def _guard_git_operation() -> None:
    """Never stage, sync, or recover somebody else's interrupted operation."""
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                   "REVERT_HEAD", "sequencer", "STATE_CONFLICT"):
        path = Path(_checked("rev-parse", "--git-path", marker))
        if not path.is_absolute():
            path = config.BASE_DIR / path
        if path.exists():
            raise RuntimeError(f"Git operation in progress ({marker}); manual recovery required")
    if _checked("ls-files", "--unmerged"):
        raise RuntimeError("Unmerged index; manual recovery required")


def _is_state(name: str) -> bool:
    return any(name == path or name.startswith(path + "/") for path in _state_paths())


def _kind(name: str) -> str:
    root = config.BASE_DIR.resolve()
    for attribute, kind in (("SETTINGS_FILE", "settings"), ("WATCHLIST_FILE", "stocks"),
                            ("SUBSCRIPTIONS_FILE", "subscriptions"), ("SEEN_FILE", "seen"),
                            ("SCHEDULE_FILE", "schedule")):
        path = Path(getattr(config, attribute)).resolve()
        if path == root / name:
            return kind
    # Conventional names also support legacy state locations.
    return {"settings.json": "settings", "watchlist.json": "stocks",
            "subscriptions.json": "subscriptions", "seen_actions.json": "seen",
            "seen.json": "seen", "schedule.json": "schedule"}.get(Path(name).name,
                "report" if "openclose" in Path(name).parts else "atomic")


def _load_state(name: str, raw: str):
    value = strict_json_loads(raw)
    # stocks, seen, and schedule are list-rooted; every other kind is dict-rooted.
    expected = list if _kind(name) in ("stocks", "seen", "schedule") else dict
    if type(value) is not expected:
        raise StateConflict(f"{name}: expected {expected.__name__} root")
    return value


def _worktree_json_files():
    files = set()
    for name in _state_paths():
        path = config.BASE_DIR / name
        if path.is_dir():
            files.update(path.rglob("*.json"))
        elif path.exists() and path.suffix == ".json":
            files.add(path)
    return sorted(files)


def _validate_worktree():
    for path in _worktree_json_files():
        if path.is_symlink():
            raise StateConflict(f"State symlink refused: {path}")
        _load_state(path.relative_to(config.BASE_DIR).as_posix(), path.read_text(encoding="utf-8"))


def _tree_state(ref):
    entries = _checked("ls-tree", "-r", "-z", ref, "--", *_state_paths())
    result = {}
    for entry in entries.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        if not name.endswith(".json"):
            continue
        if kind != "blob" or mode != "100644":
            raise StateConflict(f"Unsupported state file mode: {name}")
        result[name] = (oid, _load_state(name, _checked("cat-file", "blob", oid)))
    return result


def _validate_tree(ref):
    _tree_state(ref)


def _reconcile_state(branch):
    """Prepare a semantic tree off-index, then fast-forward to a merge commit.

    Both original tips remain parents. Until the final fast-forward, failures
    only create unreachable objects/a recovery ref: HEAD, index, and disk stay
    untouched. No reset/abort is needed, even if building the merge fails.
    """
    _guard_git_operation()
    local = _checked("rev-parse", "HEAD")
    remote = _checked("rev-parse", f"origin/{branch}")
    bases = _checked("merge-base", "--all", local, remote).splitlines()
    if len(bases) != 1:
        raise StateConflict("ambiguous merge base")
    base = bases[0]
    changed = _checked("diff", "--name-only", "--no-renames", base, local).splitlines()
    if any(not _is_state(name) or not name.endswith(".json") for name in changed):
        raise StateConflict("local non-state commits require manual reconciliation")
    trees = [_tree_state(ref) for ref in (base, local, remote)]
    merged = {}
    for name in sorted(set().union(*(tree.keys() for tree in trees))):
        values = [tree[name][1] if name in tree else MISSING for tree in trees]
        value = merge_state(_kind(name), *values, label=name, session=Path(name).stem)
        if value is not MISSING:
            payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            _load_state(name, payload)
            merged[name] = payload

    backup = f"refs/state-backups/{uuid.uuid4().hex}"
    _checked("update-ref", backup, local)
    log.info("State recovery tip retained at %s (%s)", backup, local)
    with tempfile.TemporaryDirectory(prefix="state-merge-") as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        _checked("read-tree", remote, extra_env=env)
        for name in sorted(set(trees[2]) | set(merged)):
            if name not in merged:
                _checked("update-index", "--force-remove", "--", name, extra_env=env)
            else:
                oid = _checked("hash-object", "-w", "--stdin", input_text=merged[name])
                _checked("update-index", "--add", "--cacheinfo", "100644", oid, name, extra_env=env)
        tree = _checked("write-tree", extra_env=env)
        _validate_tree(tree)
        commit = _checked(*_BOT_IDENTITY, "commit-tree", tree, "-p", local, "-p", remote,
                          "-m", "Reconcile bot state semantically")
    # merge --ff-only never invokes Git's text/content merge machinery.
    _guard_git_operation()
    _checked("merge", "--ff-only", commit)


def push_state() -> bool:
    """Commit and push watchlist/seen state back to the repo, if changed.

    Returns True when the repo is in sync (pushed, or nothing to push).
    Returns False when credentials are missing or the push failed - callers
    should NOT discard local state in that case.

    Handles concurrent writers with a strict semantic three-way merge commit.
    No Git text merge or rebase is allowed to decide domain state.

    On failure, sets the module-global `push_error` to a short reason.
    """
    global push_error
    try:
        with state_lock(), _state_git_lock:
            return _push_state_locked()
    except Exception as error:
        return _failure("State push failed", str(error))


def _push_state_locked() -> bool:
    """push_state body - caller must hold _state_git_lock."""
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

    _guard_git_operation()
    paths = _state_paths()
    _validate_worktree()
    _validate_tree("HEAD")
    staged_result = _git("git", "diff", "--cached", "--name-only")
    if staged_result.returncode:
        return _failure("git diff failed", staged_result.stderr)
    # Never accidentally include somebody else's staged code in a state commit.
    for name in staged_result.stdout.splitlines():
        if not name.endswith(".json") or not any(name == path or name.startswith(path + "/") for path in paths):
            return _failure("State push refused", "unrelated staged changes present")

    tracked = _git("git", "ls-files", "--", *paths)
    if tracked.returncode:
        return _failure("git ls-files failed", tracked.stderr)
    candidates = [path.relative_to(config.BASE_DIR).as_posix()
                  for path in _worktree_json_files()]
    candidates += [name for name in tracked.stdout.splitlines() if name.endswith(".json")]
    # Includes explicit tracked deletions, but never lock/quarantine artifacts.
    if candidates:
        # Exclude runtime locks/backups even if a directory is a state path.
        result = _git("git", "add", "-A", "--", *sorted(set(candidates)),
                      ":(exclude,glob)**/*.lock", ":(exclude,glob)**/*.bak",
                      ":(exclude,glob)**/*.tmp")
        if result.returncode:
            return _failure("git add failed", result.stderr)
    staged_result = _git("git", "diff", "--cached", "--name-only")
    if staged_result.returncode:
        return _failure("git diff failed", staged_result.stderr)
    if staged_result.stdout.strip():
        result = _git("git", *_BOT_IDENTITY, "commit", "-m", "Update bot state")
        if result.returncode:
            # Leave both index and worktree intact for the next retry.
            return _failure("git commit failed", result.stderr)

    _validate_tree("HEAD")
    push_result = _git("git", "push", remote_url, f"HEAD:{branch}")
    if push_result.returncode == 0:
        log.info("Pushed state to %s", branch)
        push_error = ""
        return True

    # Reconcile newly committed AND ahead+clean retries against a fresh ref.
    fetched = _fetch_state(remote_url, branch)
    if fetched.returncode:
        return _failure("git fetch failed after rejected push", fetched.stderr)
    dirty = _git("git", "status", "--porcelain", "--untracked-files=all")
    if dirty.returncode:
        return _failure("git status failed", dirty.stderr)
    if dirty.stdout.strip():
        return _failure("Reconciliation skipped", "uncommitted changes present; retry after saving them")
    try:
        _reconcile_state(branch)
    except Exception as error:
        return _failure("State reconciliation failed; original local commits preserved", str(error))
    _validate_tree("HEAD")
    retry_push_result = _git("git", "push", remote_url, f"HEAD:{branch}")
    if retry_push_result.returncode == 0:
        log.info("Pushed state to %s (after semantic reconciliation)", branch)
        push_error = ""
        return True
    push_error = (
        "git push failed after semantic reconciliation: "
        + (_tail(retry_push_result.stderr, 200) or "unknown error")
    )
    log.warning("Push failed after reconciliation: %s", _redact_gh(retry_push_result.stderr.strip()[-500:]))
    return False


def sync_state() -> bool:
    """Fast-forward a clean checkout while holding the storage transaction lock.

    Dirty or locally-ahead checkouts are left intact for push_state to save.
    No hard reset, autostash, or conflict-side selection is ever used.
    Returns True when synced or skipped safely; False on a Git failure.
    """
    token = os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        log.info("GH_TOKEN/GITHUB_REPOSITORY not set - skipping state sync")
        return True
    try:
        with state_lock(), _state_git_lock:
            _guard_git_operation()
            _validate_worktree()
            remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
            branch = _push_branch(remote_url)
            status = _git("git", "status", "--porcelain", "--untracked-files=all")
            if status.returncode:
                return _failure("git status failed", status.stderr)
            dirty = status.stdout.strip()
            if dirty:
                log.warning(
                    "State sync skipped: uncommitted changes present - push them "
                    "first (dirty: %s)",
                    dirty[:200],
                )
                return True
            fetched = _fetch_state(remote_url, branch)
            if fetched.returncode:
                return _failure("git fetch failed during sync", fetched.stderr)
            ahead = _git("git", "rev-list", "--count", f"origin/{branch}..HEAD")
            if ahead.returncode:
                return _failure("git rev-list failed", ahead.stderr)
            if int(ahead.stdout.strip()):
                # Local commits exist that were never pushed. A hard reset here
                # would silently destroy them - push them first instead.
                log.warning(
                    "State sync skipped: local branch is ahead of origin/%s "
                    "(unpushed commits). Run push_state or fix credentials first.",
                    branch,
                )
                return True
            _validate_tree(f"origin/{branch}")
            result = _git("git", "merge", "--ff-only", f"origin/{branch}")
        if result.returncode == 0:
            log.info("State synced from origin/%s", branch)
            return True
        return _failure("State sync failed", result.stderr)
    except Exception as error:
        return _failure("State sync failed", str(error))


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
