"""Backend update supervision and durable completion contracts.

Process launches and ownership probes are stubbed except in the marked scope test.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def action_spawn(tmp_path, monkeypatch):
    from hermes_cli import _launchers, main_dashboard, web_server, web_server_gateway as actions

    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(actions, "_ACTION_LOG_DIR", tmp_path / "logs")
    for name in ("_ACTION_PROCS", "_ACTION_RESULTS", "_ACTION_COMMANDS", "_ACTION_IDS"):
        monkeypatch.setattr(actions, name, {})
    monkeypatch.setattr(_launchers, "runtime_command", lambda root, args: [sys.executable, *args])
    monkeypatch.setattr(main_dashboard, "_get_pid_cgroup_path", lambda pid: None)
    return actions


@pytest.mark.platforms("linux")
def test_update_spawn_leaves_backend_service_cgroup(action_spawn, monkeypatch):
    from hermes_cli import main_dashboard
    from tools import process_registry as scopes

    monkeypatch.setattr(main_dashboard, "_get_pid_cgroup_path", lambda pid: "/system.slice/backend.service")
    monkeypatch.setattr(scopes, "_systemd_run_user_scope_available", lambda: True)
    calls = []
    monkeypatch.setattr(action_spawn.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or SimpleNamespace(pid=42))

    action_spawn._spawn_hermes_action(["update"], "hermes-update", env_overrides={"HERMES_ACTION_ID": "a" * 32})

    cmd, kwargs = calls[0]
    assert "--scope" in cmd and "--user" in cmd
    assert cmd[cmd.index("--") + 1:] == [sys.executable, "update"]
    assert kwargs["cwd"] == str(action_spawn._ACTION_LOG_DIR.parent)
    assert kwargs["env"]["HERMES_ACTION_ID"] == "a" * 32


@pytest.mark.platforms("linux")
def test_missing_scope_refuses_before_launch(action_spawn, monkeypatch):
    from hermes_cli import main_dashboard
    from tools import process_registry as scopes

    monkeypatch.setattr(main_dashboard, "_get_pid_cgroup_path", lambda pid: "/system.slice/backend.service")
    monkeypatch.setattr(scopes, "_systemd_run_user_scope_available", lambda: False)
    calls = []
    monkeypatch.setattr(action_spawn.subprocess, "Popen", lambda *a, **kw: calls.append(a))
    with pytest.raises(RuntimeError, match="restart-safe"):
        action_spawn._spawn_hermes_action(["update"], "hermes-update")
    assert calls == []
    assert action_spawn._ACTION_PROCS == {}


def test_unsupervised_update_keeps_direct_launch(action_spawn, monkeypatch):
    calls = []
    monkeypatch.setattr(action_spawn.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(pid=42))
    action_spawn._spawn_hermes_action(["update"], "hermes-update")
    assert calls == [[sys.executable, "update"]]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("unit", ["hermes-webui.service", None])
def test_backend_inventory_uses_exact_systemd_owner(monkeypatch, unit):
    from hermes_cli import main_dashboard, process_identity, update_inventory as inventory

    monkeypatch.setattr(process_identity, "ledger_entries", lambda: [
        {"purpose": "serve", "profile": "default", "pid": 42, "create_time": 123, "argv": "hermes serve"}])
    monkeypatch.setattr(process_identity, "spawner_is_dead", lambda row: True)
    monkeypatch.setattr(inventory, "_loaded_backend_launchd_jobs", list)
    monkeypatch.setattr(main_dashboard, "_get_systemd_service_for_pid", lambda pid: unit if pid == 42 else None)
    monkeypatch.setattr(main_dashboard, "_get_pid_cgroup_path", lambda pid: "/user.slice/app.slice/hermes-webui.service")
    plan = inventory.UpdatePlan()
    inventory._collect_ledger_runtimes(plan, set())
    row = plan.runtimes[0]
    assert row.supervisor == ("systemd" if unit else "manual-serve")
    if unit:
        assert row.detail["systemd_unit"] == unit
        assert row.detail["systemd_scope"] == "user"


@pytest.mark.parametrize("outcome,code", [("failed", 1), ("refused", 2), ("interrupted", 130), ("success", 0), ("partial", 1)])
def test_restarted_backend_recovers_terminal_receipt(action_spawn, monkeypatch, outcome, code):
    from hermes_cli import update_receipt
    from hermes_cli.web_routers import actions

    action_id = "b" * 32
    action_spawn._ACTION_LOG_DIR.mkdir()
    (action_spawn._ACTION_LOG_DIR / "hermes-update.action_id").write_text(action_id)
    monkeypatch.setenv("HERMES_ACTION_ID", action_id)
    update_receipt.begin_update_receipt()
    update_receipt.finalize_update_receipt(outcome)
    result = asyncio.run(actions.get_action_status("hermes-update"))
    assert result["exit_code"] == code
    assert result["receipt"]["action_id"] == action_id


def test_restarted_backend_keeps_live_receipt_running(action_spawn, monkeypatch):
    from hermes_cli import update_receipt
    from hermes_cli.web_routers import actions

    action_id = "d" * 32
    action_spawn._ACTION_LOG_DIR.mkdir()
    (action_spawn._ACTION_LOG_DIR / "hermes-update.action_id").write_text(action_id)
    monkeypatch.setenv("HERMES_ACTION_ID", action_id)
    update_receipt.begin_update_receipt()
    try:
        result = asyncio.run(actions.get_action_status("hermes-update"))
        assert result["running"] is True
        assert result["exit_code"] is None
    finally:
        update_receipt.finalize_update_receipt("failed")


def test_restarted_backend_reports_dead_receipt_as_failure(action_spawn, monkeypatch):
    from hermes_cli import update_receipt
    from hermes_cli.web_routers import actions

    action_id = "e" * 32
    action_spawn._ACTION_LOG_DIR.mkdir()
    (action_spawn._ACTION_LOG_DIR / "hermes-update.action_id").write_text(action_id)
    monkeypatch.setenv("HERMES_ACTION_ID", action_id)
    update_receipt.begin_update_receipt()
    monkeypatch.setattr(update_receipt, "_owner_alive", lambda record: False)
    try:
        result = asyncio.run(actions.get_action_status("hermes-update"))
        assert result["running"] is False
        assert result["exit_code"] == 1
    finally:
        update_receipt.finalize_update_receipt("failed")


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("restart_code", [0, 1])
def test_operator_named_backend_uses_existing_restart_helper(monkeypatch, restart_code):
    from hermes_cli import dashboard_procs, main_dashboard

    calls = []
    monkeypatch.setattr(main_dashboard, "_run_probe", lambda cmd, **kw: (
        calls.append(cmd) or SimpleNamespace(returncode=restart_code)))
    result = dashboard_procs._restart_killed_backends(
        [42], {42: "hermes-webui.service"},
        {42: "/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-webui.service"}, {}, {})
    assert calls == [["systemctl", "--user", "restart", "hermes-webui.service"]]
    assert result == ([] if restart_code == 0 else [42])


@pytest.mark.platforms("linux")
def test_real_unreachable_user_bus_refuses_scope(tmp_path):
    """Availability failure uses the real systemd-run probe, in a fresh process."""
    root = Path(__file__).resolve().parents[2]
    script = (
        f"import os, sys; sys.path.insert(0, {str(root)!r})\n"
        "from hermes_cli.web_server_update import prepare_update_action\n"
        "try: prepare_update_action([sys.executable, '-c', 'pass'], dict(os.environ), 'fixture-failure')\n"
        "except RuntimeError as exc:\n"
        "    assert 'restart-safe' in str(exc)\n"
        "else: raise AssertionError('unsafe update was permitted')\n")
    probe = tmp_path / "probe.py"
    probe.write_text(script)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    env = dict(os.environ, INVOCATION_ID="fixture-service", XDG_RUNTIME_DIR=str(runtime_dir),
               DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime_dir}/missing-bus")
    result = subprocess.run([sys.executable, str(probe)], env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.platforms("linux")
def test_real_scope_runs_update_child_outside_backend_cgroup(action_spawn, monkeypatch, tmp_path):
    from hermes_cli import _launchers, main_dashboard
    from tools import process_registry as scopes

    if not scopes._systemd_run_user_scope_available():
        pytest.skip("user systemd scope unavailable")
    parent_cgroup = main_dashboard._get_pid_cgroup_path(os.getpid())
    output = tmp_path / "child.json"
    script = (
        "import json, os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'cgroup': pathlib.Path('/proc/self/cgroup').read_text(), 'id': os.environ['HERMES_ACTION_ID']}))"
    )
    monkeypatch.setattr(_launchers, "runtime_command", lambda root, args: [sys.executable, "-c", script, str(output)])
    # Force the service dispatch branch; the real host scope movement is what this test proves.
    monkeypatch.setattr(main_dashboard, "_get_pid_cgroup_path", lambda pid: "/system.slice/backend-fixture.service")
    child = action_spawn._spawn_hermes_action(["update"], "hermes-update", env_overrides={"HERMES_ACTION_ID": "c" * 32})
    assert child.wait(timeout=15) == 0
    data = json.loads(output.read_text())
    assert ".scope" in data["cgroup"]
    assert data["cgroup"].strip() != f"0::{parent_cgroup}"
    assert data["id"] == "c" * 32


@pytest.mark.platforms("linux")
def test_real_scoped_child_survives_disposable_backend_service_exit(tmp_path):
    """No supervisor/launch stubs: only the update payload is a bounded fixture."""
    from tools import process_registry as scopes

    if not scopes._systemd_run_user_scope_available():
        pytest.skip("user systemd scope unavailable")
    root = Path(__file__).resolve().parents[2]
    unit = f"maintained-update-fixture-{uuid4().hex}"
    action_id = uuid4().hex
    scope_name = f"hermes-worker-update-{action_id}.scope"
    backend = tmp_path / "backend.py"
    child = tmp_path / "child.py"
    launched, done = tmp_path / "launched.json", tmp_path / "done.json"
    release = tmp_path / "release"
    child.write_text(
        "import json, os, pathlib, sys, time\n"
        "release, done = map(pathlib.Path, sys.argv[1:])\n"
        "deadline = time.monotonic() + 12\n"
        "while not release.exists() and time.monotonic() < deadline: time.sleep(.05)\n"
        "done.write_text(json.dumps({'cgroup': pathlib.Path('/proc/self/cgroup').read_text(), "
        "'id': os.environ['HERMES_ACTION_ID'], 'released': release.exists()}))\n")
    backend.write_text(
        "import json, os, pathlib, subprocess, sys, time\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from hermes_cli.web_server_update import prepare_update_action\n"
        "from hermes_cli.update_inventory import _systemd_backend_owner\n"
        f"command = [sys.executable, {str(child)!r}, {str(release)!r}, {str(done)!r}]\n"
        f"env = dict(os.environ, HERMES_ACTION_ID={action_id!r})\n"
        f"command, env = prepare_update_action(command, env, {action_id!r})\n"
        f"with open({str(tmp_path / 'child.log')!r}, 'ab') as log:\n"
        "    proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, "
        "stdout=log, stderr=log, start_new_session=True)\n"
        # Wait for scope placement before allowing systemd to tear down the backend.
        "    deadline = time.monotonic() + 8\n"
        "    while time.monotonic() < deadline:\n"
        "        cg = pathlib.Path(f'/proc/{proc.pid}/cgroup')\n"
        f"        if cg.exists() and {scope_name!r} in cg.read_text(): break\n"
        "        time.sleep(.05)\n"
        "    else: raise RuntimeError('child never moved to its scope')\n"
        f"pathlib.Path({str(launched)!r}).write_text(json.dumps(_systemd_backend_owner(os.getpid())))\n")
    cmd = ["systemd-run", "--user", "--wait", "--pipe", "--collect", "--service-type=exec", f"--unit={unit}"]
    for name in ("HOME", "HERMES_HOME", "TMPDIR"):
        cmd.append(f"--setenv={name}={os.environ[name]}")
    cmd.extend([sys.executable, str(backend)])
    try:
        result = subprocess.run(cmd, env=scopes.systemd_user_bus_env(), capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(launched.read_text())["systemd_unit"] == f"{unit}.service"
        # --wait has observed service completion; only then may the scoped child finish.
        release.touch()
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        data = json.loads(done.read_text())
        assert data["released"] is True
        assert scope_name in data["cgroup"]
        assert unit not in data["cgroup"]
        assert data["id"] == action_id
    finally:
        # Both fixtures exit on bounded deadlines, even when an assertion fails.
        release.touch()
