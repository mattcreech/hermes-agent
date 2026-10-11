"""Recorded conflict resolutions finish automatically; unfamiliar ones abort intact."""
import subprocess

import pytest

from hermes_cli import main, update_cmd


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], check=check,
                          capture_output=True, text=True)


@pytest.mark.parametrize("recorded", [True, False])
def test_recorded_resolution_completes_native_merge(tmp_path, monkeypatch, recorded):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "rerere.enabled", "true")
    git(root, "config", "rerere.autoupdate", "true")
    file = root / "content"
    file.write_text("base\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "checkout", "-b", "custom/fixture")
    file.write_text("custom\n")
    git(root, "commit", "-am", "custom")
    custom = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "checkout", "main")
    file.write_text("upstream\n")
    git(root, "commit", "-am", "upstream")
    target = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "update-ref", "refs/remotes/origin/main", target)
    git(root, "checkout", "custom/fixture")
    if recorded:
        assert git(root, "merge", "--no-edit", target, check=False).returncode == 1
        file.write_text("custom and upstream\n")
        git(root, "add", "content")
        git(root, "commit", "--no-edit")
        git(root, "reset", "--hard", custom)
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(update_cmd, "_record_stop", lambda *_: None)
    if recorded:
        update_cmd._reconcile_diverged_checkout(["git"], "main", custom, target_ref="origin/main")
        assert file.read_text() == "custom and upstream\n"
        assert git(root, "merge-base", "--is-ancestor", custom, "HEAD").returncode == 0
        assert git(root, "merge-base", "--is-ancestor", target, "HEAD").returncode == 0
    else:
        with pytest.raises(SystemExit) as failure:
            update_cmd._reconcile_diverged_checkout(["git"], "main", custom, target_ref="origin/main")
        assert failure.value.code == 1
        assert git(root, "rev-parse", "HEAD").stdout.strip() == custom
        assert file.read_text() == "custom\n"
    assert git(root, "status", "--porcelain").stdout == ""
