"""Read-only admission of the operator's required installed plugins."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess


def verify_required_plugins(root: Path) -> None:
    from hermes_constants import get_hermes_home
    from pm.environments import selected_venv, venv_python
    from pm.plugins_state import read_home_selection
    from pm.plugin_declarations import read_python_declaration

    home = get_hermes_home()
    config = read_home_selection(home) or {}
    required = (config.get("updates") or {}).get("required_plugins", [])
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        raise ValueError("updates.required_plugins must be a list of names")
    if not required:
        return
    plugins = config.get("plugins") or {}
    enabled, disabled = set(plugins.get("enabled") or []), set(plugins.get("disabled") or [])
    requirements = []
    for name in required:
        if name not in enabled or name in disabled:
            raise ValueError(f"Required plugin {name!r} is not enabled")
        requirements.extend(read_python_declaration(home / "plugins" / name).install_requirements)
    provider = (config.get("memory") or {}).get("provider")
    if "mnemosyne" in required and provider != "mnemosyne":
        raise ValueError("Required Mnemosyne memory provider is not selected")
    probe = """
import importlib.metadata, json, sys
from packaging.requirements import Requirement
for text in json.loads(sys.argv[1]):
    req = Requirement(text)
    if req.marker and not req.marker.evaluate():
        continue
    version = importlib.metadata.version(req.name)
    if not req.specifier.contains(version, prereleases=True):
        raise RuntimeError(f"required dependency {req.name} does not satisfy {req.specifier}")
print("required dependencies verified")
"""
    result = subprocess.run([str(venv_python(selected_venv(root))), "-B", "-I", "-c", probe, json.dumps(requirements)],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("Required plugin dependencies are missing or incompatible in the selected environment")
