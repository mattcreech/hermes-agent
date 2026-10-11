"""Maintained publication gates use real disposable Git; no live network or services."""
from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import update_managed as managed
from hermes_cli import update_receipt as receipts

pytestmark = pytest.mark.platforms("linux")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / "hermes"))
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q", "-b", "custom/fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "file").write_text("first")
    git(root, "add", "file")
    git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "first")
    fork = tmp_path / "fork.git"
    git(root, "clone", "--bare", str(root), str(fork))
    (root / "file").write_text("second")
    git(root, "add", "file")
    git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "second")
    (home / "hermes").mkdir()
    (home / "hermes/config.yaml").write_text(
        'updates:\n  maintained_publication:\n    enabled: true\n    branch: custom/fixture\n'
        f'    url: {managed.FORK_URL}\n')
    real_git = managed._git
    calls = []

    def local_git(source, *args):
        calls.append(args)
        return real_git(source, *(str(fork) if arg == managed.FORK_URL else arg for arg in args))

    monkeypatch.setattr(managed, "_git", local_git)
    monkeypatch.setattr(managed, "_tests", lambda *_: None)
    monkeypatch.setenv("HERMES_ACTION_ID", "a" * 32)
    receipts.begin_update_receipt(correlation_id="a" * 32)
    yield root, fork, calls
    receipts._current.set(None)


def terminal():
    receipts.finalize_pending_update_receipt(0)
    return receipts.read_latest_receipt()


def test_opt_out_has_no_git_or_test_work(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("updates:\n  maintained_publication:\n    enabled: false\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(managed, "_identity", lambda *_: pytest.fail("opt-out probed Git"))
    assert managed.begin(tmp_path) is None
    assert managed.finish(tmp_path, None, native_ok=False)


def test_success_publishes_captured_commit_nonforced_and_recovers_action(checkout):
    root, fork, calls = checkout
    state = managed.begin(root)
    assert managed.finish(root, state, native_ok=True)
    assert git(fork, "rev-parse", "refs/heads/custom/fixture") == state["sha"]
    assert ("push", "--no-force", managed.FORK_URL,
            f"{state['sha']}:refs/heads/custom/fixture") in calls
    assert git(root, "status", "--porcelain") == ""
    record = terminal()
    assert record["managed_publication"]["fork_sha"] == state["sha"]
    from hermes_cli.web_routers.actions import _latest_update_receipt_summary, get_action_status
    from hermes_cli import web_server_gateway as gw
    gw._ACTION_IDS["hermes-update"] = "a" * 32
    try:
        summary = _latest_update_receipt_summary("a" * 32)
        assert summary["managed_publication"] == record["managed_publication"]
        response = asyncio.run(get_action_status("hermes-update"))
        assert response["receipt"]["action_id"] == "a" * 32
        assert response["receipt"]["managed_publication"]["status"] == "success"
    finally:
        gw._ACTION_IDS.pop("hermes-update", None)


@pytest.mark.parametrize("fault", ["branch", "url", "dirty", "head", "tests", "build", "push", "readback"])
def test_failures_are_durable_and_block_publication(checkout, monkeypatch, fault):
    root, fork, calls = checkout
    old_fork = git(fork, "rev-parse", "HEAD")
    if fault == "branch":
        git(root, "checkout", "-qb", "custom/other")
    if fault == "url":
        from hermes_cli.config import load_config
        cfg = load_config()
        cfg["updates"]["maintained_publication"]["url"] = "https://github.com/NousResearch/hermes-agent.git"
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    if fault == "dirty":
        (root / ".task").mkdir()
        (root / ".task/private").write_text("fixture private data")
    state = managed.begin(root)
    if fault == "head":
        git(root, "reset", "--hard", "HEAD~1")
    if fault == "tests":
        def fail_tests(*_):
            raise RuntimeError("behavior tests failed")
        monkeypatch.setattr(managed, "_tests", fail_tests)
    local_git = managed._git

    def fail_git(source, *args):
        if fault == "push" and args[0] == "push":
            raise RuntimeError("command exited 1")
        if fault == "readback" and args[0] == "ls-remote" and any(c[0] == "push" for c in calls):
            return f"{old_fork}\trefs/heads/custom/fixture"
        return local_git(source, *args)

    monkeypatch.setattr(managed, "_git", fail_git)
    assert not managed.finish(root, state, native_ok=fault != "build")
    record = terminal()
    assert record["managed_publication"]["status"] == "failed"
    assert managed.restart_allowed(state) == (fault in ("push", "readback"))
    assert any(row["name"] == "managed" and row["outcome"] == "failed" for row in record["stages"])
    assert any(row["step"] == "managed_publication" for row in record["followups"])
    from hermes_cli.web_routers.actions import _latest_update_receipt_summary
    assert _latest_update_receipt_summary("a" * 32)["managed_publication"]["status"] == "failed"
    assert not any(c[0] in ("add", "commit") for c in calls)
    if fault != "readback":
        assert git(fork, "rev-parse", "HEAD") == old_fork
    if fault == "dirty":
        assert (root / ".task/private").read_text() == "fixture private data"
        assert git(root, "diff", "--cached", "--name-only") == ""


def test_canonical_runner_environment_is_fresh_per_file(tmp_path, monkeypatch):
    python = Path(sys.executable)
    root = tmp_path / "source"
    root.mkdir()
    profile = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    for path in managed.TEST_FILES:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    calls = []

    def run(root, argv, *, env, timeout, failure_log):
        calls.append((argv, env, timeout))
        assert "INVOCATION_ID" not in env and "OPENAI_API_KEY" not in env
        assert Path(env["HOME"]).is_dir()
        assert str(root) not in env["HOME"]
        assert env["HERMES_PYTHON"] == str(python)
        assert argv[:2] == ["/bin/bash", "scripts/run_tests.sh"]
        assert all(test in argv for test in managed.TEST_FILES)
        assert argv[argv.index("--file-retries") + 1] == "1"
        assert failure_log.is_relative_to(profile / "logs/maintained-tests")

    monkeypatch.setattr(managed, "_run", run)
    def ensure(source, extras):
        assert source == root and extras == []
        return python
    monkeypatch.setattr("pm.testenv.ensure_testenv", ensure)
    managed._tests(root)
    assert all(Path(env["HOME"]).is_relative_to(profile / "cache/scratch") for _, env, _ in calls)
    assert not list((profile / "cache/scratch").iterdir())
    assert len(calls) == 1
    assert len({env["HOME"] for _, env, _ in calls}) == len(calls)


def test_failed_verification_keeps_readable_diagnostics(tmp_path):
    log = tmp_path / "logs" / "verification.log"
    with pytest.raises(RuntimeError, match="diagnostics:") as failure:
        managed._run(tmp_path, [sys.executable, "-c", "print('FAILED behavior contract'); raise SystemExit(1)"],
                     failure_log=log)
    assert str(log) in str(failure.value)
    assert "FAILED behavior contract" in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


def test_test_timeout_reaps_own_process(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        managed._run(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)


@pytest.mark.parametrize("build_ok", [True, False])
def test_current_native_completion_gates_after_build_before_restart(checkout, monkeypatch, build_ok):
    from hermes_cli import update_cmd, source_completion, venv_sync
    from hermes_cli.update_completion import _complete_selected

    root, _fork, calls = checkout
    events = []
    verified = []

    def tail(*args, followups, **kwargs):
        events.append("build")
        if not build_ok:
            followups.append(("build", "fixture build failure"))
        return build_ok

    monkeypatch.setattr(source_completion, "complete_source_checkout", tail)
    monkeypatch.setattr(update_cmd, "_sweep_bytecode_after_update", lambda *_: None)
    monkeypatch.setattr(venv_sync, "clear_completion", lambda *_: None)
    monkeypatch.setattr(update_cmd, "_fleet_restart_skip_reason", lambda *_: None)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update", lambda *_: events.append("restart"))
    monkeypatch.setattr(update_cmd, "_resume_windows_gateways_and_merge_outcome", lambda *_: None)
    monkeypatch.setattr(update_cmd, "_verify_fleet_after_update", lambda *a, **kw: verified.append(kw["update_complete"]))
    monkeypatch.setattr(managed, "_tests", lambda *_: events.append("tests"))
    request = {"source": str(root), "branch": "main", "sibling_snapshots": [], "plan": None,
               "desktop": False, "assume_yes": True, "gateway_mode": False,
               "snapshot_id": None, "pre_update_version": None, "windows_resume": None,
               "managed_publication_required": True}
    assert _complete_selected(request)
    assert events == (["build", "tests", "restart"] if build_ok else ["build"])
    assert any(row[0] == "push" for row in calls) == build_ok
    assert verified == ([True] if build_ok else [])
    assert terminal()["managed_publication"]["status"] == ("success" if build_ok else "failed")


def test_dirty_files_created_by_tests_are_never_staged(checkout, monkeypatch):
    root, fork, calls = checkout
    previous = git(fork, "rev-parse", "HEAD")
    state = managed.begin(root)
    monkeypatch.setattr(managed, "_tests", lambda *_: (root / "private-token").write_text("fixture"))
    assert not managed.finish(root, state, native_ok=True)
    assert git(fork, "rev-parse", "HEAD") == previous
    assert git(root, "diff", "--cached", "--name-only") == ""
    assert not any(row[0] == "push" for row in calls)


def test_dependency_failure_records_managed_block_before_selected_interpreter(checkout):
    from hermes_cli.update_completion import _record_managed_blocked

    root, _fork, calls = checkout
    _record_managed_blocked({"managed_publication_required": True, "expected_sha": git(root, "rev-parse", "HEAD")},
                           "dependencies")
    record = terminal()
    assert record["managed_publication"]["stage"] == "dependencies"
    assert any(row["name"] == "managed" and row["outcome"] == "failed" for row in record["stages"])
    assert not any(row[0] == "push" for row in calls)


def test_tracked_task_artifacts_refuse_even_a_clean_tree(checkout):
    root, _fork, calls = checkout
    (root / ".task").mkdir()
    (root / ".task/private").write_text("fixture")
    git(root, "add", ".task/private")
    git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture accidental private commit")
    state = managed.begin(root)
    assert not managed.finish(root, state, native_ok=True)
    assert not any(row[0] == "push" for row in calls)


def test_wrong_native_target_refuses_publication(checkout):
    root, _fork, calls = checkout
    state = managed.begin(root, expected_sha="f" * 40)
    assert not managed.finish(root, state, native_ok=True)
    assert terminal()["managed_publication"]["status"] == "failed"
    assert not any(row[0] == "push" for row in calls)


@pytest.mark.parametrize("evidence", ["marker", "result", "process", "running", "running-result", "running-process", "foreign"])
def test_correlated_managed_failure_overrides_native_success_only_after_completion(tmp_path, monkeypatch, evidence):
    from hermes_cli import web_server_gateway as gw
    from hermes_cli import logs
    from hermes_cli.web_routers.actions import get_action_status
    from types import SimpleNamespace

    action = "a" * 32
    update_log = tmp_path / "update.log"
    update_log.write_text(f"=== hermes-update completed {action} ===\n")
    (tmp_path / "hermes-update.log").write_text(f"=== hermes-update started now {action} ===\n")
    monkeypatch.setattr(logs, "log_file_path", lambda *_: update_log)
    monkeypatch.setattr(gw, "_ACTION_LOG_DIR", tmp_path)
    monkeypatch.setattr(gw, "_ACTION_IDS", {"hermes-update": action})
    monkeypatch.setattr(gw, "_ACTION_RESULTS", {"hermes-update": {"exit_code": 0}} if evidence in ("result", "running-result") else {})
    monkeypatch.setattr(gw, "_ACTION_COMMANDS", {})
    proc = SimpleNamespace(pid=123, poll=lambda: 0, wait=lambda **_: 0)
    monkeypatch.setattr(gw, "_ACTION_PROCS", {"hermes-update": proc} if evidence in ("process", "running-process") else {})
    record = {"action_id": action if evidence != "foreign" else "b" * 32,
              "outcome": "running" if evidence.startswith("running") else "success", "exit_code": 0,
              "managed_publication": {"status": "failed", "stage": "tests"}}
    monkeypatch.setattr(receipts, "read_receipt_for_action", lambda *_: record)
    monkeypatch.setattr(receipts, "_owner_alive", lambda *_: True)
    response = asyncio.run(get_action_status("hermes-update"))
    assert response["exit_code"] == (None if evidence.startswith("running") else 0 if evidence == "foreign" else 1)
    assert response["running"] == (evidence.startswith("running"))


@pytest.mark.parametrize("fault", ["tests", "push"])
def test_gate_failure_defers_behavior_restart_but_publication_debt_can_restart(checkout, monkeypatch, fault):
    from hermes_cli import update_cmd, source_completion, venv_sync
    from hermes_cli.update_completion import _complete_selected

    root, _fork, _calls = checkout
    events = []
    monkeypatch.setattr(source_completion, "complete_source_checkout", lambda *a, **kw: True)
    monkeypatch.setattr(update_cmd, "_sweep_bytecode_after_update", lambda *_: None)
    monkeypatch.setattr(venv_sync, "clear_completion", lambda *_: None)
    monkeypatch.setattr(update_cmd, "_fleet_restart_skip_reason", lambda *_: None)
    monkeypatch.setattr(update_cmd, "_restart_gateway_fleet_after_update", lambda *_: events.append("restart"))
    monkeypatch.setattr(update_cmd, "_resume_windows_gateways_and_merge_outcome", lambda *_: None)
    monkeypatch.setattr(update_cmd, "_verify_fleet_after_update", lambda *a, **kw: None)
    if fault == "tests":
        monkeypatch.setattr(managed, "_tests", lambda *_: (_ for _ in ()).throw(RuntimeError("fixture test failure")))
    else:
        local = managed._git
        def push_failure(source, *args):
            if args[0] == "push":
                raise RuntimeError("command exited 1")
            return local(source, *args)
        monkeypatch.setattr(managed, "_git", push_failure)
    request = {"source": str(root), "branch": "main", "sibling_snapshots": {}, "plan": None,
               "desktop": False, "assume_yes": True, "gateway_mode": False,
               "snapshot_id": None, "pre_update_version": None, "windows_resume": None,
               "managed_publication_required": True}
    assert _complete_selected(request)
    assert events == ([] if fault == "tests" else ["restart"])
    record = terminal()
    assert record["managed_publication"]["status"] == "failed"
    assert any(row["step"] == "gateway_restart" for row in record["followups"]) == (fault == "tests")
    assert request.get("managed_restart_deferred", False) == (fault == "tests")


def test_frozen_opt_in_cannot_silently_disable_after_swap(checkout, monkeypatch):
    root, _fork, calls = checkout
    monkeypatch.setattr('hermes_cli.config.load_config', dict)
    state = managed.begin(root, required=True)
    assert state is not None
    assert not managed.finish(root, state, native_ok=True)
    assert not managed.restart_allowed(state)
    assert terminal()['managed_publication']['stage'] == 'admission'
    assert not any(row[0] == 'push' for row in calls)


@pytest.mark.parametrize('tests_ok', [True, False])
def test_historical_completion_uses_supplied_gate_before_restart(checkout, monkeypatch, tests_ok):
    from hermes_cli import update_cmd, source_stamp, venv_sync
    from hermes_cli.update_finish import finish_update
    root, _fork, _calls = checkout
    events = []
    monkeypatch.setattr(update_cmd, '_run_post_update_maintenance', lambda **kw: True)
    monkeypatch.setattr(source_stamp, 'write_source_stamp', lambda *_: None)
    monkeypatch.setattr(venv_sync, 'clear_completion', lambda *_: None)
    monkeypatch.setattr(update_cmd, '_restart_gateway_fleet_after_update', lambda *_: events.append('restart'))
    monkeypatch.setattr(update_cmd, '_resume_windows_gateways_and_merge_outcome', lambda *_: None)
    monkeypatch.setattr(update_cmd, '_verify_fleet_after_update', lambda *a, **kw: None)
    if not tests_ok:
        def failed(*_):
            raise RuntimeError('fixture failed behavior')
        monkeypatch.setattr(managed, '_tests', failed)
    assert finish_update(root=root, assume_yes=True, gateway_mode=False, pre_update_snapshot_id=None,
                         had_desktop_app_before_update=False, pre_update_version=None, plan=None,
                         windows_resume=None, managed_state=managed.begin(root)) == tests_ok
    assert events == (['restart'] if tests_ok else [])
    assert terminal()['managed_publication']['status'] == ('success' if tests_ok else 'failed')


@pytest.mark.parametrize('required,stage,held', [(False, 'tests', False), (True, 'dependencies', True),
                                              (True, 'completion', True), (True, 'tests', True),
                                              (True, 'publication', False), (True, 'readback', False)])
def test_bootstrap_failure_transport_settles_only_unverified_managed_resume(tmp_path, required, stage, held):
    import json
    from hermes_cli.update_completion import _write_bootstrap_result
    token = {'resume_needed': True, 'platform': 'posix', 'pause_id': 'fixture'}
    request = {'receipt': {'update_id': 'fixture'}, 'windows_resume': token,
               'managed_publication_required': required}
    receipt = {'outcome': 'success', 'managed_publication': {'status': 'failed', 'stage': stage}}
    result = tmp_path / 'result.json'
    assert _write_bootstrap_result(request, result, 0, receipt) == 0
    record = json.loads(result.read_text())
    assert record['windows_resume'] == (token if held else None)
    assert record['receipt']['outcome'] == 'success'
    assert token['resume_needed']


@pytest.mark.parametrize('required', [False, True])
def test_native_parent_leaves_managed_failed_paused_set_owed(checkout, tmp_path, monkeypatch, required):
    import json
    from copy import deepcopy
    from hermes_cli import main, update_cmd
    from hermes_cli.update_completion import _record_managed_blocked, _write_bootstrap_result
    root, _fork, _calls = checkout
    token = {'resume_needed': True, 'platform': 'posix', 'pause_id': 'fixture'}
    request = {'receipt': deepcopy(receipts._current.get().data), 'windows_resume': token,
               'managed_publication_required': required, 'expected_sha': git(root, 'rev-parse', 'HEAD')}
    _record_managed_blocked(request, 'dependencies')
    record = terminal()
    result_path = tmp_path / 'bootstrap-result.json'
    _write_bootstrap_result(request, result_path, 0, record)
    result = json.loads(result_path.read_text())
    events = []
    monkeypatch.setattr(update_cmd, '_unrestored_autostash_notice', lambda: None)
    monkeypatch.setattr(update_cmd, '_write_fleet_restart_pending_marker', lambda **_: None)
    monkeypatch.setattr(update_cmd, 'run_completion', lambda *_: result)
    monkeypatch.setattr(update_cmd, '_accept_completion_pm_receipt', lambda *_: None)
    monkeypatch.setattr(main, '_resume_windows_gateways_after_update', lambda *_: events.append('resume'))
    update_cmd._complete_source_update(request)
    assert events == ([] if required else ['resume'])
    assert request['windows_resume_settled']
    assert token['resume_needed']


def test_timeout_keeps_partial_diagnostics(tmp_path):
    log = tmp_path / "timeout.log"
    with pytest.raises(RuntimeError, match="verification timed out"):
        managed._run(tmp_path, [sys.executable, "-u", "-c",
                     "import time; print('started behavior tests'); time.sleep(30)"],
                     timeout=0.1, failure_log=log)
    assert "started behavior tests" in log.read_text()
    assert "exceeded its deadline" in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


def test_failed_admission_remains_held_across_unrelated_launches(checkout, monkeypatch):
    from hermes_cli import update_pause_record

    root, _fork, _calls = checkout
    state = managed.begin(root)
    assert managed.restart_held()
    monkeypatch.setattr(managed, "_tests", lambda *_: (_ for _ in ()).throw(RuntimeError("behavior failure")))
    assert not managed.finish(root, state, native_ok=True)
    assert managed.restart_held()
    monkeypatch.setattr(update_pause_record, "retire_redundant", lambda: pytest.fail("startup bypassed admission"))
    update_pause_record.recover(["config", "get", "model"])
    assert managed.restart_held()
    monkeypatch.setattr(managed, "_tests", lambda *_: None)
    state = managed.begin(root)
    assert managed.finish(root, state, native_ok=True)
    assert not managed.restart_held()
