"""Catch up a prepared feed within the native update, without an agent outer loop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid

from hermes_cli.update_prepared_notes import PreparationRequired

RESULT_PREFIX = "HERMES_PREPARED_RESULT="


class PreparationHeld(PreparationRequired):
    """Keep the coordinator's offline handoff in this update's receipt."""

    def __init__(self, result: dict):
        self.result = result
        decision = result.get("repair_decision") or {}
        reason = str(result.get("reason") or "The candidate could not be repaired and verified")
        summary = decision.get("summary") or decision.get("reason")
        super().__init__(reason + (f"; Codex: {summary}" if summary else ""))


def upstream_tip(root: Path, git: str = "git") -> str:
    from hermes_cli.source_check import _branch_tip, OFFICIAL_REPOSITORY

    tip, _missing, failure = _branch_tip(OFFICIAL_REPOSITORY, "main", root, git, "upstream")
    if not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{40}", tip):
        raise PreparationRequired(f"Could not check official upstream: {failure or 'main is unavailable'}")
    return tip


def pending_status(root: Path, settings: dict, result: dict, git: str) -> None:
    """Discovery is read-only; normal Update prepares a captured upstream tip."""
    if not settings.get("prepare_command"):
        return
    tip = upstream_tip(root, git)
    result["upstreamSha"] = tip
    pending = result["preparedValidation"]["upstream_sha"] != tip
    result["upstreamPending"] = pending
    if pending:
        result.update(updateAvailable=True, behind=-1,
                      message="New upstream changes will be prepared, repaired if needed, and verified during this Update.")


def offer_preparation(root: Path, settings: dict, result: dict, checkout, reason: str, git: str) -> bool:
    """A stale proof needs fresh preparation, without granting source admission."""
    if not settings.get("prepare_command") or checkout.dirty or checkout.current_branch != settings["checkout_branch"]:
        return False
    tip = upstream_tip(root, git)
    _command(settings, tip)
    result.pop("error", None)
    result.pop("preparedValidation", None)
    result.update(upstreamPending=True, preparationPending=True, upstreamSha=tip,
                  updateAvailable=True, behind=-1, preparationReason=reason,
                  message="Compatibility checks need refreshing. Update will prepare, repair if needed, and verify before installation.")
    return True


def _command(settings: dict, requested: str) -> list[str]:
    command = settings.get("prepare_command")
    if (not isinstance(command, list) or not command or len(command) > 64
            or any(not isinstance(arg, str) or not arg or "\n" in arg or "\x00" in arg for arg in command)
            or not Path(command[0]).is_absolute()):
        raise PreparationRequired("This installation has no valid on-demand preparation command")
    return [*command, "--on-demand", "--upstream-sha", requested, "--lock-timeout", "0"]


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)  # windows-footgun: ok — coordinator admission is POSIX-only
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok — coordinator admission is POSIX-only
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def _stream(command: list[str], root: Path, home: Path, log: Path, *, timeout: float | None = None) -> dict:
    """Observe preparation until completion or explicit caller cancellation/timeout."""
    from hermes_cli.source_check import source_git_env
    from hermes_platform.host.facts import os_family

    if os_family() not in ("linux", "darwin"):
        raise PreparationRequired("On-demand preparation currently supports Linux and macOS installations")
    env = {**source_git_env(), "HERMES_HOME": str(home), "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    deadline, buffer, result = (time.monotonic() + timeout if timeout is not None else None), b"", None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        with log.open("xb") as output:
            log.chmod(0o600)
            while selector.get_map():
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise PreparationRequired("Preparation exceeded the explicitly requested timeout; the working version was kept")
                if not selector.select(min(20, remaining) if remaining is not None else 20):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                output.write(chunk)
                output.flush()
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace")
                    if line.startswith(RESULT_PREFIX):
                        if result is not None:
                            raise PreparationRequired("Preparation returned more than one completion result")
                        result = json.loads(line[len(RESULT_PREFIX):])
                    else:
                        print(line, flush=True)
            if buffer:
                print(buffer.decode("utf-8", errors="replace"), flush=True)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()) if deadline is not None else None)
        if not isinstance(result, dict) or result.get("schema_version") != 1:
            raise PreparationRequired(f"Preparation did not return a completion record (exit {code})")
        if code != 0 or result.get("status") != "ready":
            raise PreparationHeld(result)
        return result
    finally:
        selector.close()
        process.stdout.close()
        _stop(process)


def ensure_current(root: Path) -> str | None:
    """Prepare before native backup, drain or source movement, under its update lock."""
    from hermes_cli.update_prepared import policy
    from hermes_cli.update_receipt import record_fact, record_stage
    from hermes_cli.update_cmd_common import _record_stop

    settings = policy(root)
    if settings is None or not settings.get("prepare_command"):
        return None
    home = Path(settings["owner_home"])
    directory = home / "logs" / "prepared-updates"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    log = directory / ("request-" + uuid.uuid4().hex + ".log")
    try:
        requested = upstream_tip(root)
        print(f"→ Checking and preparing upstream {requested[:10]} before installation…", flush=True)
        record_stage("preparation", "running", sha=requested)
        record_fact("preparation_request", {"requested_upstream_sha": requested, "log": str(log)})
        result = _stream(_command(settings, requested), root, home, log)
        if result.get("requested_upstream_sha") != requested or not re.fullmatch(r"[0-9a-f]{40}", result.get("ready_sha", "")):
            raise PreparationRequired("Preparation result does not identify the requested source")
        record_fact("preparation_result", {**result, "log": str(log)})
        record_stage("preparation", "success", sha=result["ready_sha"])
        return requested
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = f"Update preparation stopped: {exc}. Details: {log}"
        print(f"✗ {reason}", flush=True)
        record_fact("preparation_result", {**(exc.result if isinstance(exc, PreparationHeld) else {}),
                                           "status": "held", "reason": str(exc), "log": str(log)})
        record_stage("preparation", "failed")
        _record_stop("prepared_source_required")
        raise SystemExit(1) from exc


def prepare_and_refresh_plan(root: Path, plan):
    """Long preparation must not freeze stale runtime start/stop intent."""
    requested = ensure_current(root)
    if requested is not None:
        from hermes_cli.update_inventory import collect_runtime_inventory, record_plan_in_receipt
        from hermes_cli.update_receipt import record_stage
        plan = collect_runtime_inventory()
        record_plan_in_receipt(plan)
        record_stage("plan_refreshed", "success")
    return plan, requested
