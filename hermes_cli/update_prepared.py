"""Opt-in installation authority for consuming only prepared, verified source commits.

The Git-directory binding identifies the owning config independently of the caller's
profile. Git, PM, backups, custody and restarts remain owned by the native updater.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from hermes_cli.update_prepared_notes import PreparationRequired, validate_note

POLICY_FILENAME = "hermes-prepared-policy.json"
TRACKING_NOTES_REF = "refs/notes/hermes-prepared-consumed"


def _git(root: Path, *args: str, timeout: int = 60, check: bool = True) -> str:
    from hermes_cli.source_check import source_git_env
    from hermes_cli.update_custody import run_git
    from hermes_platform.resolver import locate_command

    command = locate_command("git").command
    if not command:
        raise PreparationRequired("Git is unavailable for prepared-source verification")
    result = run_git(command, list(args), cwd=root, capture_output=True, text=True,
                     encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                     env=source_git_env(), timeout=timeout)
    if check and result.returncode:
        raise PreparationRequired("Prepared-source Git verification failed; the working version was kept")
    return result.stdout.strip() if result.returncode == 0 else ""


def policy_path(root: Path) -> Path:
    common = Path(_git(root, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else root / common).resolve() / POLICY_FILENAME


def _settings(config: dict, root: Path) -> dict | None:
    from hermes_cli.update_channel import install_id

    updates = config.get("updates") or {}
    if not isinstance(updates, dict):
        raise PreparationRequired("updates must be a mapping for prepared-source policy")
    values = updates.get("prepared_channels") or {}
    if not isinstance(values, dict):
        raise PreparationRequired("updates.prepared_channels must be an installation mapping")
    entry = values.get(install_id(root))
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise PreparationRequired("Prepared installation policy is unreadable")
    return entry


def bind_policy(root: Path, owner_home: Path) -> Path:
    """Bind an already configured installation to its owning home; deployment only."""
    from hermes_cli._early_recovery import write_durable_text
    from hermes_cli.config import require_readable_config_before_write

    root, owner_home = root.resolve(), owner_home.resolve()
    entry = _settings(require_readable_config_before_write(owner_home / "config.yaml"), root)
    if not entry or entry.get("enabled") is not True:
        raise PreparationRequired("Configure this installation's prepared policy before binding it")
    path = policy_path(root)
    write_durable_text(path, json.dumps({"schema_version": 1, "source_root": str(root),
                                        "owner_home": str(owner_home)}))
    path.chmod(0o600)
    return path


def policy(root: Path) -> dict | None:
    """Read physical-install authority, never inherit another profile's channel choice."""
    from hermes_constants import get_hermes_home
    from hermes_cli.config import require_readable_config_before_write
    from hermes_cli.update_managed import FORK_URL

    root = root.resolve()
    if not (root / ".git").exists():
        return None
    path = policy_path(root)
    if not path.exists():
        config_path = get_hermes_home() / "config.yaml"
        config = require_readable_config_before_write(config_path) if config_path.exists() else {}
        entry = _settings(config, root)
        if entry and entry.get("enabled") is True:
            raise PreparationRequired("Prepared-source policy requires an installation authority binding")
        return None
    try:
        binding = json.loads(path.read_text(encoding="utf-8-sig"))
        if (not isinstance(binding, dict) or type(binding.get("schema_version")) is not int
                or binding["schema_version"] != 1
                or binding.get("source_root") != str(root)):
            raise PreparationRequired("Prepared-source authority does not match this installation")
        owner_home = Path(binding["owner_home"])
        if not owner_home.is_absolute() or str(owner_home.resolve()) != binding["owner_home"]:
            raise PreparationRequired("Prepared-source owning home is invalid")
        entry = _settings(require_readable_config_before_write(owner_home / "config.yaml"), root)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, PreparationRequired):
            raise
        raise PreparationRequired("Prepared-source installation authority is unreadable") from exc
    if not entry or entry.get("enabled") is not True:
        raise PreparationRequired("Bound prepared-source policy was removed or disabled")
    if (entry.get("url") != FORK_URL or not str(entry.get("branch", "")).startswith("prepared/")
            or not str(entry.get("checkout_branch", "")).startswith("custom/")
            or entry.get("notes_ref") != "refs/notes/hermes-prepared"):
        raise PreparationRequired("Prepared-source policy is outside the approved fork and branch scope")
    for key in ("branch", "checkout_branch"):
        _git(root, "check-ref-format", f"refs/heads/{entry[key]}")
    if _git(root, "remote", "get-url", "origin") != entry["url"]:
        raise PreparationRequired("Prepared-source origin differs from the configured fork")
    return {**entry, "owner_home": str(owner_home)}


def prepare_args(root: Path, args) -> None:
    """Make every CLI/API trigger select the same prepared branch before side effects."""
    from hermes_cli.update_cmd_common import _record_stop

    try:
        settings = policy(root)
        if settings is None:
            return
        if (getattr(args, "branch", None) not in (None, settings["branch"])
                or getattr(args, "channel", None) not in (None, "main")
                or getattr(args, "set_channel", None) not in (None, "main")
                or getattr(args, "switch_branch", False)):
            raise PreparationRequired("This installation updates only from its verified prepared feed")
        args.branch = settings["branch"]
    except (PreparationRequired, OSError, subprocess.SubprocessError) as exc:
        print(f"✗ Preparation required: {exc}")
        _record_stop("prepared_source_required", without_receipt="refused")
        raise SystemExit(2) from exc


def guard_channel(root: Path, channel: str) -> None:
    settings = policy(root)
    if settings is not None and channel != "main":
        raise PreparationRequired("This installation's prepared feed owns source selection; channel switching is disabled")


def cli_check(root: Path, branch: str, *, branch_explicit: bool, channel: str | None) -> bool:
    """Native CLI preview must make the same attestation decision as the UI check."""
    from hermes_constants import get_hermes_home

    settings = policy(root)
    if settings is None:
        return False
    if (branch_explicit and branch != settings["branch"]) or channel not in (None, "main"):
        raise PreparationRequired("This installation checks only its verified prepared feed")
    status = check_status(root, get_hermes_home(), settings, force=True)
    if status.get("error"):
        print(status["message"])
        raise SystemExit(1)
    print(f"→ Prepared update feed: origin/{settings['branch']}")
    print(status["message"] if status.get("upstreamPending") else
          "✓ Already up to date." if status["behind"] == 0 else "⚕ A verified prepared update is available.")
    return True


def admit_target(root: Path, request: dict, settings: dict) -> str:
    """Verify the fetched SHA and its note before native code may stash, switch or move."""
    if _git(root, "symbolic-ref", "--short", "HEAD") != settings["checkout_branch"]:
        raise PreparationRequired("Prepared update requires the configured maintained checkout branch")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PreparationRequired("Prepared update found local source changes; the working version was kept")
    target = _git(root, "rev-parse", "--verify", f"refs/remotes/origin/{settings['branch']}^{{commit}}")
    _git(root, "merge-base", "--is-ancestor", "HEAD", target)
    _git(root, "fetch", "--no-filter", "--no-tags", "origin", f"+{settings['notes_ref']}:{TRACKING_NOTES_REF}", timeout=120)
    note = validate_note(_git(root, "notes", f"--ref={TRACKING_NOTES_REF}", "show", target), target)
    verify_runtime_note(root, settings, note)
    for key in ("upstream_sha", "custom_sha", "previous_ready_sha"):
        if note.get(key):
            _git(root, "merge-base", "--is-ancestor", note[key], target)
    if request.get("requested_upstream_sha"):
        _git(root, "merge-base", "--is-ancestor", request["requested_upstream_sha"], target)
    request.update(expected_sha=target, prepared_sha=target, prepared_validation=note)
    return target


def verify_runtime_note(root: Path, settings: dict, note: dict, *, current_digest: str | None = None) -> None:
    """Runtime proof is scoped to this physical installation and its current declarations."""
    from hermes_cli.update_prepared_inputs import runtime_input_digest
    from hermes_cli.update_channel import install_id

    expected = (note["input_digests"].get(install_id(root)) if "input_digests" in note else
                note["input_digest"] if note.get("owner_install_id") == install_id(root) else None)
    if expected is None:
        raise PreparationRequired("Prepared candidate has no runtime-validation proof for this installation")
    current_digest = current_digest or runtime_input_digest(root, Path(settings["owner_home"]))
    if current_digest != expected:
        raise PreparationRequired("Runtime plugin, feature or memory settings changed since preparation; waiting for fresh validation")


def admit_request(root: Path, request: dict) -> None:
    """Report a feed refusal at the native boundary, before any checkout mutation."""
    from hermes_cli.update_cmd_common import _record_stop

    if not request.get("prepared_policy"):
        return
    try:
        admit_target(root, request, request["prepared_policy"])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"✗ Preparation required: {exc}")
        _record_stop("prepared_source_required")
        raise SystemExit(1) from exc
    from hermes_cli.update_receipt import record_fact, record_stage
    record_fact("prepared_source", {"status": "verified", "branch": request["prepared_policy"]["branch"],
                                    **request["prepared_validation"]})
    record_stage("prepared_admission", "success", sha=request["prepared_sha"])


def check_status(root: Path, home: Path, settings: dict, *, git: str = "git", force: bool = False) -> dict:
    """Advertise only the fork's attested branch tip; no fallback to raw main."""
    from hermes_cli import source_check as source
    from hermes_cli.update_channel import install_id
    from hermes_cli.update_prepared_notes import remote_note
    from hermes_cli.update_prepared_inputs import runtime_input_digest

    import time
    co = source._read_checkout(root, git, None)
    result = {"supported": True, "hermesRoot": str(root), "currentSha": co.head,
              "currentBranch": co.current_branch, "dirty": co.dirty, "branch": settings["branch"],
              "prepared": True, "behind": None, "commits": [], "updateAvailable": False}
    try:
        current_digest = runtime_input_digest(root, Path(settings["owner_home"]))
        identity = {"root": str(root), "head": co.head, "origin": co.origin, "requestProtocol": 1,
                    "settings": settings, "inputDigest": current_digest}
        cache = Path(settings["owner_home"]) / "source-checks" / f"{install_id(root)}.prepared.json"
        now = time.time()
        cached = source._read_json(cache)
        if (not force and isinstance(cached, dict) and cached.get("identity") == identity
                and isinstance(cached.get("ts"), (int, float)) and 0 <= now - cached["ts"] < 120
                and isinstance(cached.get("status"), dict)):
            return {**cached["status"], "dirty": co.dirty, "currentBranch": co.current_branch}
        target, _missing, failure = source._branch_tip(co.repository, settings["branch"], root, git)
        if target is None:
            raise PreparationRequired(f"Prepared source is unavailable: {failure or 'branch is not published'}")
        if co.repository is None:
            raise PreparationRequired("Prepared source has no GitHub fork authority")
        previous = cached.get("status") if isinstance(cached, dict) and cached.get("identity") == identity else None
        if not isinstance(previous, dict) or previous.get("targetSha") != target or previous.get("error"):
            note = remote_note(co.repository, settings["notes_ref"], target)
        else:
            note = previous["preparedValidation"]
        verify_runtime_note(root, settings, note, current_digest=current_digest)
        behind, commits = source._behind_count(co, target)
        result.update(targetSha=target, behind=behind, commits=commits, updateAvailable=behind != 0,
                      fetchedAt=int(now * 1000), preparedValidation=note)
        from hermes_cli.update_prepared_request import pending_status
        pending_status(root, settings, result, git)
        source._write_cache(cache, identity, now, result)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        from hermes_cli.update_prepared_request import offer_preparation
        try:
            if offer_preparation(root, settings, result, co, str(exc), git):
                return result
        except (OSError, ValueError, subprocess.SubprocessError) as source_error:
            exc = source_error
        result.update(error="preparation-required", behind=None, updateAvailable=False,
                      message=f"Preparation required: {exc}")
    return result
