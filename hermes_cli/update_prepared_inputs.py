"""Credential-free runtime declarations shared by preparation and update admission."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "cache", "state", "data", "logs", "sessions",
    "memories", "backups", "dist", "build",
})
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log")
MNEMOSYNE_SETTINGS = (
    "llm_enabled", "host_llm_enabled", "host_llm_provider", "host_llm_model", "host_llm_n_ctx",
    "sleep_model_refresh_enabled", "sleep_model_refresh_auto_apply", "sleep_prompt", "sleep_batch",
    "llm_conflict_detection", "smart_compress",
)


def canonical_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def plugin_file_paths(directory: Path) -> list[Path]:
    """Whole plugin code and assets, without live data, secrets, caches or symlink escapes."""
    files = []
    for current, dirs, names in os.walk(directory, followlinks=False):
        parent = Path(current)
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS
                         and not name.endswith(".egg-info") and not (parent / name).is_symlink())
        for name in sorted(names):
            path = parent / name
            if (name == ".env" or name.startswith(".env.") or name.endswith(EXCLUDED_SUFFIXES)
                    or path.is_symlink()):
                continue
            if path.is_file():
                files.append(path)
    return sorted(files, key=lambda path: path.relative_to(directory).as_posix())


def plugin_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in plugin_file_paths(directory):
        digest.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _mnemosyne_settings(home: Path) -> dict:
    import utils

    path = home / "mnemosyne" / "config.yaml"
    if not path.is_file():
        return {}
    config = utils.fast_safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("Mnemosyne runtime settings must be a mapping")
    return {key: config[key] for key in MNEMOSYNE_SETTINGS if key in config}


def _profile_inputs(home: Path, owner_home: Path) -> dict:
    from pm.plugins_state import read_home_selection

    config = read_home_selection(home) or {}
    plugins, memory = config.get("plugins") or {}, config.get("memory") or {}
    required = (config.get("updates") or {}).get("required_plugins") or []
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise ValueError("Required plugin declarations must be a list of names")
    return {"home": str(home.resolve()), "name": "default" if home == owner_home else home.name,
            "enabled": list(plugins.get("enabled") or []), "disabled": list(plugins.get("disabled") or []),
            "required_plugins": sorted(set(required)), "memory_provider": memory.get("provider") or "",
            "mnemosyne_config": _mnemosyne_settings(home)}


def _plugin_input(directory: Path, root: Path, entries: list[tuple]) -> dict:
    from hermes_cli.update_prepared import _git

    directory = directory.resolve()
    scope = "external"
    if directory.is_relative_to(root / "plugins"):
        relative = directory.relative_to(root)
        tracked = set(_git(root, "ls-files", "--", relative.as_posix()).splitlines())
        paths = plugin_file_paths(directory)
        scope = "tracked-bundled" if paths and all(path.relative_to(root).as_posix() in tracked for path in paths) else "untracked-bundled"
    row = {"identity": str(directory), "scope": scope, "selection_keys": [
        {"home": str(plugins.parent.resolve()), "key": key}
        for plugins, key, path in entries if path.resolve() == directory]}
    # The exact candidate commit binds tracked source; only external overlays are inputs.
    if scope != "tracked-bundled":
        row["digest"] = plugin_digest(directory)
    return row


def runtime_inputs(root: Path, owner_home: Path) -> dict:
    """Read the same complete profile/plugin union as PM, in its explicit owning scope."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from pm.environments import runtime_facts_path
    from pm.lock import Facts
    from pm.plugins_state import dependency_homes
    from pm.workspace import enabled_plugin_dirs, enabled_plugin_entries

    root, owner_home = root.resolve(), owner_home.resolve()
    token = set_hermes_home_override(owner_home)
    try:
        homes = dependency_homes()
        profiles = [_profile_inputs(home, owner_home) for home in homes]
        entries = enabled_plugin_entries(source_root=root)
        directories = enabled_plugin_dirs(source_root=root)
        venv = Facts(runtime_facts_path(root)).get("venv") or {}
        extras = venv.get("extras", [])
        if not isinstance(extras, list) or any(not isinstance(name, str) for name in extras):
            raise ValueError("Native dependency extras must be a list of names")
        return {"schema_version": 1, "extras": sorted(set(extras)), "profiles": profiles,
                "plugins": [_plugin_input(directory, root, entries) for directory in directories]}
    finally:
        reset_hermes_home_override(token)


def runtime_input_digest(root: Path, owner_home: Path) -> str:
    return canonical_digest(runtime_inputs(root, owner_home))
