"""Keep generated plugin graphs scoped to the requested core features.

uv resolves declared optional dependencies even when sync will not install them.
An unused extra must not evict an enabled plugin. Selected core packages still
use the vetted core lock, including all of their platform-specific branches.
"""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
import tomllib

from pm.package import InstallError


def select_core_features(document: dict, core_lock: Path, extras: list[str]) -> set[str] | None:
    requested = set(extras)
    optional = document.get("project", {}).get("optional-dependencies", {})
    removed = set(optional) - requested
    document["project"]["optional-dependencies"] = {key: value for key, value in optional.items()
                                                     if key in requested}
    hermes = document.get("tool", {}).get("hermes", {})
    if "opt-in-extras" in hermes:
        hermes["opt-in-extras"] = [extra for extra in hermes["opt-in-extras"] if extra not in removed]
    uv = document.get("tool", {}).get("uv", {})
    if "conflicts" in uv:
        uv["conflicts"] = [group for group in uv["conflicts"]
                           if not any(item.get("extra") in removed and
                                      item.get("package", document["project"]["name"]) == document["project"]["name"]
                                      for item in group)]
    if not core_lock.is_file():
        return None
    entries = defaultdict(list)
    for package in tomllib.loads(core_lock.read_text(encoding="utf-8-sig")).get("package", []):
        entries[package["name"]].append(package)
    root_name = document["project"]["name"]
    if root_name not in entries:
        raise InstallError("venv", "core lock does not contain its root project")
    queue = deque([({"name": root_name}, requested)])
    selected, visited = set(), set()
    while queue:
        edge, enabled = queue.popleft()
        selected.add(edge["name"])
        for package in entries[edge["name"]]:
            if any(key in edge and package.get(key) != edge[key] for key in ("version", "source")):
                continue
            identity = (package["name"], package.get("version"), str(package.get("source")), tuple(sorted(enabled)))
            if identity in visited:
                continue
            visited.add(identity)
            dependencies = list(package.get("dependencies", []))
            for extra in enabled:
                dependencies.extend(package.get("optional-dependencies", {}).get(extra, []))
            queue.extend((dependency, set(dependency.get("extra", []))) for dependency in dependencies)
    return selected
