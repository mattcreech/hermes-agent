"""A code gate cannot certify a disabled or missing required capability."""
from pathlib import Path
import sys

import pytest

from hermes_cli.update_capabilities import verify_required_plugins


@pytest.mark.parametrize("disabled,provider", [(True, "mnemosyne"), (False, "")])
def test_required_memory_cannot_pass_with_disabled_plugin_or_cleared_provider(tmp_path, monkeypatch, disabled, provider):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(f"updates:\n  required_plugins: [mnemosyne]\nplugins:\n  enabled: [mnemosyne]\n"
                      f"  disabled: {'[mnemosyne]' if disabled else '[]'}\nmemory:\n  provider: '{provider}'\n")
    plugin = tmp_path / "plugins" / "mnemosyne"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text('[project]\nname="memory"\nversion="1"\ndependencies=[]\n')
    with pytest.raises(ValueError, match="Required"):
        verify_required_plugins(tmp_path)


@pytest.mark.parametrize("missing", [False, True])
def test_required_dependencies_are_checked_in_selected_interpreter(tmp_path, monkeypatch, missing):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("pm.environments.selected_venv", lambda root: Path(sys.prefix))
    config = tmp_path / "config.yaml"
    config.write_text("updates:\n  required_plugins: [mnemosyne]\nplugins:\n  enabled: [mnemosyne]\nmemory:\n  provider: mnemosyne\n")
    plugin = tmp_path / "plugins" / "mnemosyne"
    plugin.mkdir(parents=True)
    requirement = "never-installed-required-addon==1" if missing else "pytest>=7,<10"
    (plugin / "pyproject.toml").write_text(f'[project]\nname="memory"\nversion="1"\ndependencies=["{requirement}"]\n')
    before = config.read_bytes()
    if missing:
        with pytest.raises(ValueError, match="dependencies"):
            verify_required_plugins(tmp_path)
    else:
        verify_required_plugins(tmp_path)
    assert config.read_bytes() == before
