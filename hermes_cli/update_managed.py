"""Opt-in maintained-branch verification and exact fork publication at completion.

Git selection, dependencies, builds and restarts remain the native updater's work.
Publication never forces a push. Opt-in repair uses a separately verified isolated clone.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

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
    "tests/hermes_cli/test_update_managed_repair.py",
    "tests/hermes_cli/test_maintained_update_integration.py",
    "tests/hermes_cli/test_update_maintained_profile_scope.py",
    "tests/hermes_cli/test_update_merge_resolution.py",
    "tests/hermes_cli/test_update_managed_diagnostics.py",
    "tests/hermes_cli/test_maintained_backend_update.py",
    "tests/pm/test_required_plugin_policy.py",
    "tests/pm/test_workspace.py",
    "tests/pm/test_workspace_optional_conflicts.py",
    "tests/pm/test_workspace_build_inputs.py",
    "tests/hermes_cli/test_update_capabilities.py",
)


def _run(root: Path, argv: list[str], *, env=None, timeout=60, failure_log: Path | None = None) -> str:
    # No raw Git errors in receipts: credential helpers can include private diagnostics.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except BaseException as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — begin admits Linux only
            proc.wait(timeout=10)
            if failure_log is not None and isinstance(exc, subprocess.TimeoutExpired):
                output.seek(0)
                failure_log.parent.mkdir(parents=True, exist_ok=True)
                failure_log.write_bytes(output.read() + b"\nMaintained verification exceeded its deadline.\n")
                failure_log.chmod(0o600)
                raise RuntimeError(f"verification timed out; diagnostics: {failure_log}") from exc
            raise
        if code:
            if failure_log is not None:
                output.seek(0)
                failure_log.parent.mkdir(parents=True, exist_ok=True)
                failure_log.write_bytes(output.read())
                failure_log.chmod(0o600)
                detail = next((line.strip() for line in failure_log.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                               if line.startswith("FAILED ")), f"command exited {code}")
                raise RuntimeError(f"{detail[:300]}; diagnostics: {failure_log}")
            raise RuntimeError(f"command exited {code}")
        output.seek(0)
        return output.read().decode("utf-8", "replace").strip()


def _git(root: Path, *args: str) -> str:
    from hermes_cli.update_cmd import _base_git_cmd

    return _run(root, [*_base_git_cmd(), *args], env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                timeout=600 if args and args[0] == "push" else 60)


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



def _restart_hold_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "maintained-tests" / "restart-hold.json"


def restart_held() -> bool:
    """Startup recovery must not bypass an unfinished maintained admission."""
    return _restart_hold_path().exists()


def _hold_restart(sha: str | None) -> None:
    path = _restart_hold_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"restart-hold-{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps({"sha": sha, "reason": "maintained verification pending"}), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _release_restart() -> None:
    with contextlib.suppress(FileNotFoundError):
        _restart_hold_path().unlink()

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
    try:
        _hold_restart(state["sha"])
    except OSError as exc:
        state["error"] = f"cannot persist maintained restart admission: {exc}"
    record_fact("managed_publication", {"sha": state["sha"], "status": "pending"})
    record_stage("managed", "running", sha=state["sha"] or "")
    return state


def _tests(root: Path, *, interpreter: Path | None = None) -> None:
    from hermes_constants import get_hermes_home
    from pm.testenv import ensure_testenv

    interpreter = interpreter or ensure_testenv(root, extras=[])
    # Minimal locked runtime + dev/test groups cover the carried Telegram, prompt and
    # provider contracts. No optional audio/browser/ML extras are needed for this gate.
    scratch_root = get_hermes_home() / "cache" / "scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    diagnostics = get_hermes_home() / "logs" / "maintained-tests" / f"{time.time_ns()}-{os.getpid()}.log"
    print(f"  → Verifying maintained behavior ({len(TEST_FILES)} isolated test files; one transient retry)…", flush=True)
    with tempfile.TemporaryDirectory(prefix="maintained-tests-", dir=scratch_root) as directory:
        scratch = Path(directory)
        for test in TEST_FILES:
            if not (root / test).is_file():
                raise ValueError(f"canonical behavior test missing: {test}")
        (scratch / "tmp").mkdir()
        from pm import uv_launcher

        uv = uv_launcher("uv")
        if uv is None:
            raise RuntimeError("PM-managed uv is required for dependency contract verification")
        tool_path = str(uv.parent) + os.pathsep + "/usr/local/bin:/usr/bin:/bin"
        env = {"PATH": tool_path, "HOME": str(scratch),
               "HERMES_HOME": str(scratch / "hermes"), "TMPDIR": str(scratch / "tmp"),
               "HERMES_PYTHON": str(interpreter), "TZ": "UTC", "LANG": "C.UTF-8",
               "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "0"}
        # The canonical runner isolates each file and retries a failed file once
        # in a fresh process. Timeouts/signals are never retried.
        command = ["/bin/bash", "scripts/run_tests.sh", *TEST_FILES, "-j", "2",
                   "--file-timeout", "180", "--file-retries", "1", "--tb=short"]
        try:
            _run(root, command, env=env, timeout=1800, failure_log=diagnostics)
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"maintained behavior verification: {type(exc).__name__}") from exc


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
        try:
            _tests(root)
        except RuntimeError as exc:
            from hermes_cli.update_managed_repair import repair_tests

            if not repair_tests(root, state, exc):
                raise
            sha = state["sha"]
        record_stage("managed_tests", "success", sha=sha)
        stage = "required_capabilities"
        from hermes_cli.update_capabilities import verify_required_plugins

        verify_required_plugins(root)
        record_stage("required_capabilities", "success", sha=sha)
        stage = "checkout"
        if _identity(root, settings) != sha:
            raise ValueError("HEAD changed during maintained verification")
        _release_restart()
        ref = f"refs/heads/{settings['branch']}"
        stage = "publication"
        # Ref must already exist: this is publication to the approved existing branch.
        if not _git(root, "ls-remote", "--exit-code", settings["url"], ref):
            raise ValueError("existing fork branch was not found")
        stage = "checkout"
        if _identity(root, settings) != sha:
            raise ValueError("HEAD changed before maintained publication")
        stage = "publication"
        print("  → Publishing verified customizations to the existing fork…", flush=True)
        record_stage("managed_publication", "running", sha=sha)
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
