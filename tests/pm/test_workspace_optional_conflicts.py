"""An unused feature cannot displace an enabled plugin; real conflicts refuse."""
from pathlib import Path
import subprocess
import sys

import pytest

from pm.environment import managed_environment
import pm.workspace as ws
from tests.pm._fixtures import _wheel
from tests.pm.test_workspace import layout as layout  # noqa: PLC0414 -- fixture import
from tests.pm.test_environment_build import locked_project as locked_project  # noqa: PLC0414 -- fixture import


def conflicting_feature(layout, locked_project):
    tmp, core, plugin, _ = layout
    _, uv, env = locked_project
    _wheel(tmp / "wheels", "other_dep", "2.0")
    manifest = core / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace('other=["other-dep==1.0"]', 'other=["other-dep==2.0"]'))
    subprocess.run([str(uv), "lock", "--python", sys.executable], cwd=core, env=env,
                   check=True, capture_output=True)
    manifest = plugin / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace('"member-dep==1.0"', '"other-dep==1.0"'))
    return tmp, core, plugin


def test_unselected_extra_cannot_displace_enabled_plugin(layout, locked_project):
    tmp, core, plugin = conflicting_feature(layout, locked_project)
    before = (core / "pyproject.toml").read_bytes(), (core / "uv.lock").read_bytes()
    environment = managed_environment(tmp / "candidate")
    ws.lock_and_sync([plugin], ["chosen"], root=tmp / "workspace", source=core,
                     seed_lock=core / "uv.lock", environment=environment)
    result = subprocess.run([str(environment.executable), "-I", "-c",
                             "import importlib.metadata as m; print(m.version('other-dep'), m.version('base-dep'), m.version('chosen-dep'))"],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "1.0 1.0 1.0"
    assert before == ((core / "pyproject.toml").read_bytes(), (core / "uv.lock").read_bytes())


@pytest.mark.parametrize("core_conflict", [False, True])
def test_selected_or_required_core_conflict_refuses_without_install(layout, locked_project, core_conflict):
    tmp, core, plugin = conflicting_feature(layout, locked_project)
    if core_conflict:
        manifest = plugin / "pyproject.toml"
        _wheel(tmp / "wheels", "base_dep", "2.0")
        manifest.write_text(manifest.read_text().replace('"other-dep==1.0"', '"base-dep==2.0"'))
    environment = managed_environment(tmp / "candidate")
    with pytest.raises(ws.ResolutionConflict):
        ws.lock_and_sync([plugin], ["other"], root=tmp / "workspace", source=core,
                         seed_lock=core / "uv.lock", environment=environment)
    assert not (tmp / "candidate").exists()
