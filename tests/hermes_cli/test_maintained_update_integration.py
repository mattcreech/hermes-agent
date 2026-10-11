"""Real Git integration for user-triggered maintained updates.

Uses the upstream flow fixture: backup, bytecode maintenance and platform
pause/resume are stubbed. Completion (dependencies/build/restart) is intercepted;
the branch guard, fetch, merge, syntax admission and rollback use real Git.
"""
from types import SimpleNamespace

import pytest

from hermes_cli import main, update_cmd
from tests.hermes_cli.test_update_parked_branch_guard import (
    _git, _patch_update_flow, repo_pair as repo_pair,  # noqa: PLC0414 — imported pytest fixture
)


class CompletionReached(Exception):
    pass


@pytest.fixture
def maintained_repo(repo_pair, monkeypatch):
    from hermes_cli import config

    _git(repo_pair, "branch", "-m", "custom/matt-maintained")
    (repo_pair / "maintained.txt").write_text("preserve me\n")
    _git(repo_pair, "add", "maintained.txt")
    _git(repo_pair, "commit", "-qm", "maintained behavior")
    _patch_update_flow(monkeypatch, repo_pair)
    # The runtime now retargets commands to their owning PM install. This real
    # Git fixture owns its command; it must never retarget to the test Python's
    # host installation before exercising the native update flow.
    monkeypatch.setattr("hermes_cli.update_owning_install.retarget_to_owning_install", lambda root: None)
    monkeypatch.setattr(config, "load_config", lambda: {
        "updates": {"parked_branch_strategy": "update_in_place"}})
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda *a, **k: (
        _ for _ in ()).throw(CompletionReached()))
    return repo_pair


def apply_update():
    main.cmd_update(SimpleNamespace(branch=None, yes=True, force=False, force_venv=False))


def test_two_upstream_advances_preserve_maintained_branch(maintained_repo):
    repo = maintained_repo
    origin = repo.parent / "origin"
    custom_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    for sequence in (1, 2):
        name = f"advance-{sequence}.txt"
        (origin / name).write_text(f"upstream {sequence}\n")
        _git(origin, "add", name)
        _git(origin, "commit", "-qm", f"advance {sequence}")
        target = _git(origin, "rev-parse", "HEAD").stdout.strip()
        with pytest.raises(CompletionReached):
            apply_update()
        assert _git(repo, "branch", "--show-current").stdout.strip() == "custom/matt-maintained"
        assert _git(repo, "merge-base", "--is-ancestor", custom_sha, "HEAD").returncode == 0
        assert _git(repo, "merge-base", "--is-ancestor", target, "HEAD").returncode == 0
        assert (repo / "maintained.txt").read_text() == "preserve me\n"
        assert (repo / name).read_text() == f"upstream {sequence}\n"
        assert _git(repo, "status", "--porcelain").stdout == ""


def test_conflict_refuses_and_restores_maintained_tree(maintained_repo):
    repo = maintained_repo
    (repo / "a.txt").write_text("maintained conflicting change\n")
    _git(repo, "commit", "-aqm", "maintained conflict")
    previous = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(SystemExit) as failure:
        apply_update()
    assert failure.value.code == 1
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == previous
    assert _git(repo, "branch", "--show-current").stdout.strip() == "custom/matt-maintained"
    assert (repo / "a.txt").read_text() == "maintained conflicting change\n"
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_dirty_maintained_tree_refuses_without_committing(maintained_repo):
    repo = maintained_repo
    previous = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "private.txt").write_text("uncommitted private fixture\n")
    with pytest.raises(SystemExit) as failure:
        apply_update()
    assert failure.value.code == 1
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == previous
    assert (repo / "private.txt").read_text() == "uncommitted private fixture\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == "?? private.txt"
