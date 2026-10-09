"""Restart-safe launch preparation for backend-triggered updates."""
import os


def prepare_update_action(command: list[str], env: dict[str, str], action_id: str | None):
    """Use the existing user-scope machinery when the backend inherits a service cgroup.

    setsid only detaches the terminal. KillMode can still terminate every child in
    that service, including the fresh-checkout completion process.
    """
    from hermes_platform.host.facts import os_family
    from hermes_cli.main_dashboard import _get_pid_cgroup_path

    if os_family() != "linux":
        return command, env
    cgroup = _get_pid_cgroup_path(os.getpid()) or ""
    if not env.get("INVOCATION_ID") and not any(part.endswith(".service") for part in cgroup.split("/")):
        return command, env
    from tools.process_registry import (
        _build_systemd_scope_argv, _systemd_run_user_scope_available, systemd_user_bus_env,
    )

    if not _systemd_run_user_scope_available():
        raise RuntimeError("Cannot start update: a restart-safe user systemd scope is unavailable.")
    scoped = _build_systemd_scope_argv(command, unit_suffix=f"update-{action_id or os.getpid()}")
    if scoped == command:
        raise RuntimeError("Cannot start update: restart-safe systemd-run disappeared before launch.")
    return scoped, systemd_user_bus_env(env)
