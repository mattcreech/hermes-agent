"""Required capabilities survive the update's normal plugin quarantine policy."""
from pathlib import Path

import pytest

from pm.package import InstallError
from pm.plugin_eviction import PluginEviction


def test_required_plugin_cannot_be_evicted_from_its_own_profile(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin = home / "plugins" / "memory"
    plugin.mkdir(parents=True)
    config = home / "config.yaml"
    config.write_text("plugins:\n  enabled: [memory]\nmemory:\n  provider: memory\nupdates:\n  required_plugins: [memory]\n")
    before = config.read_bytes()
    monkeypatch.setattr("pm.publication.selection_snapshot", lambda: {config: before})
    with pytest.raises(InstallError, match="Required plugin.*memory"):
        PluginEviction([(plugin.parent, "memory", plugin)], {plugin.resolve(): "dependency conflict"})
    assert config.read_bytes() == before


def test_optional_plugin_keeps_native_quarantine_behavior(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    plugin = home / "plugins" / "optional"
    plugin.mkdir(parents=True)
    config = home / "config.yaml"
    config.write_text("plugins:\n  enabled: [optional]\nupdates:\n  required_plugins: [memory]\n")
    before = config.read_bytes()
    monkeypatch.setattr("pm.publication.selection_snapshot", lambda: {config: before})
    change = PluginEviction([(plugin.parent, "optional", plugin)], {plugin.resolve(): "dependency conflict"})
    assert config.read_bytes() == before
    assert b"disabled:" in change.edits[0][2] and b"optional" in change.edits[0][2]
