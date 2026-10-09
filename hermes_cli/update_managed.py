"""Opt-in maintained-branch verification and exact fork publication at completion.

Git selection, dependencies, builds and restarts remain the native updater's work.
This gate never adds files, commits, fetches, changes branches or forces a push.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile

FORK_URL = "https://github.com/mattcreech/hermes-agent.git"
TEST_FILES = (
    "tests/gateway/test_model_picker_persist.py",
    "tests/gateway/test_telegram_model_picker.py",
    "tests/agent/test_i18n.py",
    "tests/agent/test_prompt_builder.py",
    "tests/hermes_cli/test_anthropic_picker_curated.py",
    "tests/hermes_cli/test_source_check.py",
    "tests/hermes_cli/test_source_check_maintained.py",
    "tests/hermes_cli/test_update_managed.py",
)


def _run(root: Path, argv: list[str], *, env=None, timeout=60) -> str:
    # No raw Git errors in receipts: credential helpers can include private diagnostics.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — begin admits Linux only
            proc.wait(timeout=10)
            raise
        if code:
            raise RuntimeError(f"command exited {code}")
        output.seek(0)
        return output.read().decode("utf-8", "replace").strip()


def _git(root: Path, *args: str) -> str:
    from hermes_cli.update_cmd import _base_git_cmd

    return _run(root, [*_base_git_cmd(), *args], env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _identity(root: Path, settings: dict) -> str:
    if settings.get("url") != FORK_URL:
        raise ValueError("maintained publication URL is not the allowed existing fork")
    branch = settings.get("branch")
    if not isinstance(branch, str) or not branch.startswith("custom/"):
        raise ValueError("maintained publication requires an explicit custom branch")
    _git(root, "check-ref-format", f"refs/heads/{branch}")
    if _git(root, "symbolic-ref", "--short", "HEAD") != branch:
        raise ValueError("checkout is not on the configured maintained branch")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("maintained checkout has tracked or untracked changes")
    if _git(root, "ls-files", "--", ".task"):
        raise ValueError("task-private artifacts are tracked in the maintained checkout")
    return _git(root, "rev-parse", "HEAD")


def begin(root: Path, *, expected_sha: str | None = None, required: bool = False) -> dict | None:
    """Capture the selected SHA before native completion work; opt-out is a no-op."""
    from hermes_cli.config import load_config
    from hermes_cli.update_receipt import record_fact, record_stage

    settings = (load_config().get("updates") or {}).get("maintained_publication") or {}
    if settings.get("enabled") is not True and not required:
        return None
    from hermes_platform.host.facts import os_family

    state = {"settings": settings, "sha": None, "error": None}
    try:
        if settings.get("enabled") is not True:
            raise ValueError("required maintained publication was disabled during the update")
        if os_family() != "linux":
            raise ValueError("maintained publication currently requires Linux")
        state["sha"] = _identity(root, settings)
        if expected_sha and state["sha"] != expected_sha:
            raise ValueError("selected checkout HEAD differs from the native update target")
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        state["error"] = str(exc)
    record_fact("managed_publication", {"sha": state["sha"], "status": "pending"})
    record_stage("managed", "running", sha=state["sha"] or "")
    return state


def _tests(root: Path) -> None:
    from hermes_constants import get_hermes_home
    from pm.testenv import ensure_testenv

    interpreter = ensure_testenv(root, extras=[])
    # Minimal locked runtime + dev/test groups cover the carried Telegram, prompt and
    # provider contracts. No optional audio/browser/ML extras are needed for this gate.
    scratch_root = get_hermes_home() / "cache" / "scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maintained-tests-", dir=scratch_root) as directory:
        scratch = Path(directory)
        for index, test in enumerate(TEST_FILES):
            if not (root / test).is_file():
                raise ValueError(f"canonical behavior test missing: {test}")
            home = scratch / str(index)
            home.mkdir()
            (home / "tmp").mkdir()
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
                   "HERMES_HOME": str(home / "hermes"), "TMPDIR": str(home / "tmp"),
                   "TZ": "UTC", "LANG": "C.UTF-8",
                   "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "0"}
            # Fresh canonical per-file pytest, retaining conftest/Git guards. The shell
            # runner overrides TMPDIR to /var/tmp; this bounded gate owns all its scratch.
            command = [str(interpreter), "-m", "pytest", test,
                       "--basetemp", str(home / "pytest"), "-q"]
            try:
                _run(root, command, env=env, timeout=180)
            except (RuntimeError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"{test}: {type(exc).__name__}") from exc


def finish(root: Path, state: dict | None, *, native_ok: bool) -> bool:
    """Persist a separate managed verdict before native fleet restart can kill a backend."""
    if state is None:
        return True
    from hermes_cli.update_receipt import record_fact, record_followup, record_stage

    stage = "admission"
    settings, sha = state["settings"], state["sha"]
    try:
        if state["error"]:
            raise ValueError(state["error"])
        stage = "native_completion"
        if not native_ok:
            raise ValueError("native dependencies/build/maintenance did not complete safely")
        stage = "tests"
        _tests(root)
        record_stage("managed_tests", "success", sha=sha)
        stage = "checkout"
        if _identity(root, settings) != sha:
            raise ValueError("HEAD changed during maintained verification")
        ref = f"refs/heads/{settings['branch']}"
        stage = "publication"
        # Ref must already exist: this is publication to the approved existing branch.
        if not _git(root, "ls-remote", "--exit-code", settings["url"], ref):
            raise ValueError("existing fork branch was not found")
        stage = "checkout"
        if _identity(root, settings) != sha:
            raise ValueError("HEAD changed before maintained publication")
        stage = "publication"
        _git(root, "push", "--no-force", settings["url"], f"{sha}:{ref}")
        stage = "readback"
        if _git(root, "ls-remote", "--exit-code", settings["url"], ref).split() != [sha, ref]:
            raise ValueError("fork readback does not match the verified commit")
        stage = "checkout"
        if _identity(root, settings) != sha:
            raise ValueError("checkout changed during maintained publication")
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        state["failed_stage"] = stage
        record_stage(f"managed_{stage}", "failed")
        record_fact("managed_publication", {"sha": sha, "status": "failed", "stage": stage})
        record_stage("managed", "failed", stage=stage, sha=sha or "")
        record_followup("managed_publication", f"{stage}: {exc}",
                        retry="resolve the reported gate, then run `hermes update` again")
        return False
    record_fact("managed_publication", {"sha": sha, "status": "success", "fork_sha": sha})
    record_stage("managed", "success", sha=sha)
    return True


def restart_allowed(state: dict | None) -> bool:
    """Publication debt may restart verified code; failed admission/behavior may not."""
    return state is None or state.get("failed_stage") in (None, "publication", "readback")
