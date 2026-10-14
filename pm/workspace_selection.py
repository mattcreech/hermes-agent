"""Limit plugin workspaces to requested core features without changing dependency bounds.

Universal resolution includes unused extras. Remove those declarations only in
fresh workspace metadata; upstream owns compatible transitive version policy.
"""
from __future__ import annotations


def trim_unrequested_core_extras(document: dict, extras: list[str]) -> None:
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
