"""Prepared-feed custody using real disposable Git graphs and validation notes."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import subprocess

import pytest

from hermes_cli import update_prepared as prepared
from hermes_cli.update_prepared_notes import PreparationRequired, VALIDATION_STAGES, remote_note, validate_gates


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(root, text):
    (root / "feature").write_text(text)
    git(root, "add", "feature")
    git(root, "-c", "commit.gpgsign=false", "commit", "-qm", text)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def feed(tmp_path, monkeypatch):
    from hermes_cli.update_channel import install_id
    from hermes_cli.update_managed import FORK_URL

    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "consumer"
    root.mkdir()
    git(root, "init", "-qb", "custom/fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    base = commit(root, "maintained")
    fork = tmp_path / "fork.git"
    git(root, "clone", "--bare", str(root), str(fork))
    producer = tmp_path / "producer"
    git(root, "clone", str(fork), str(producer))
    git(producer, "config", "user.email", "fixture@example.invalid")
    git(producer, "config", "user.name", "Fixture")
    git(producer, "checkout", "-qb", "prepared/fixture")
    target = commit(producer, "verified upstream change")
    note = {"schema_version": 1, "candidate_sha": target, "custom_sha": base,
            "upstream_sha": target, "previous_ready_sha": base, "input_digest": "a" * 64,
            "contracts_digest": "b" * 64, "validated_at": "2026-10-14T12:00:00Z",
            "validation": dict.fromkeys(VALIDATION_STAGES, "pass")}
    note["owner_install_id"] = install_id(root)
    settings = {"enabled": True, "url": FORK_URL, "branch": "prepared/fixture",
                "checkout_branch": "custom/fixture", "notes_ref": "refs/notes/hermes-prepared"}
    git(root, "remote", "add", "origin", FORK_URL)
    (home / "config.yaml").write_text(json.dumps({
        "updates": {"prepared_channels": {install_id(root): settings}}}))
    from hermes_cli.update_prepared_inputs import runtime_input_digest
    note["input_digest"] = runtime_input_digest(root, home)
    prepared.bind_policy(root, home)
    real_git = prepared._git

    def local_git(source, *args, **kwargs):
        # Transport only is adapted; config authority, ancestry and Git notes are real.
        if args and args[0] == "fetch":
            args = tuple(str(fork) if value == "origin" else value for value in args)
        return real_git(source, *args, **kwargs)

    monkeypatch.setattr(prepared, "_git", local_git)
    state = argparse.Namespace(root=root, home=home, fork=fork, producer=producer,
                               base=base, target=target, note=note, settings=settings)
    publish(state)
    git(root, "fetch", str(fork), "+refs/heads/prepared/fixture:refs/remotes/origin/prepared/fixture")
    return state


def publish(feed):
    git(feed.producer, "notes", "--ref=refs/notes/hermes-prepared", "add", "-f", "-m",
        json.dumps(feed.note), feed.note["candidate_sha"])
    git(feed.producer, "push", "--atomic", str(feed.fork),
        "HEAD:refs/heads/prepared/fixture", "refs/notes/hermes-prepared:refs/notes/hermes-prepared")


def test_admitted_feed_fast_forwards_exact_commit_without_branch_switch(feed, monkeypatch):
    from hermes_cli import main, update_cmd

    monkeypatch.setattr(main, "PROJECT_ROOT", feed.root)
    monkeypatch.setattr(update_cmd, "_record_stop", lambda *_: None)
    request = {}
    before = git(feed.root, "branch", "--show-current")
    target = prepared.admit_target(feed.root, request, prepared.policy(feed.root))
    update_cmd._move_checkout_to(["git"], feed.settings["branch"],
                                f"origin/{feed.settings['branch']}", target, feed.base, strict_ff=True)
    assert git(feed.root, "rev-parse", "HEAD") == target == request["expected_sha"]
    assert git(feed.root, "branch", "--show-current") == before == "custom/fixture"
    assert git(feed.root, "status", "--porcelain") == ""


@pytest.mark.platforms("linux")
def test_linux_backend_note_admits_without_macos_build(feed):
    feed.note["validation"]["macos_build"] = "not_requested"
    publish(feed)
    request = {}
    assert prepared.admit_target(feed.root, request, prepared.policy(feed.root)) == feed.target
    assert request["prepared_validation"]["validation"]["macos_build"] == "not_requested"


@pytest.mark.platforms("linux")
def test_backend_proof_does_not_require_desktop_tests_or_build(feed):
    for stage in ("desktop_tests", "desktop_types", "macos_build"):
        feed.note["validation"][stage] = "not_requested"
    publish(feed)
    assert prepared.admit_target(feed.root, {}, prepared.policy(feed.root)) == feed.target


def test_gate_validation_requires_a_passed_macos_build_for_darwin():
    validation = dict.fromkeys(VALIDATION_STAGES, "pass")
    validate_gates(validation, "darwin")
    validation["macos_build"] = "not_requested"
    validate_gates(validation, "linux")
    with pytest.raises(PreparationRequired, match="every required validation gate"):
        validate_gates(validation, "darwin")


@pytest.mark.parametrize("fault", ["missing", "malformed", "wrong-sha", "failed-gate", "bad-ancestor"])
def test_unverified_note_never_moves_consumer(feed, fault):
    if fault == "missing":
        git(feed.producer, "notes", "--ref=refs/notes/hermes-prepared", "remove", feed.target)
        git(feed.producer, "push", str(feed.fork), "refs/notes/hermes-prepared")
    elif fault == "malformed":
        git(feed.producer, "notes", "--ref=refs/notes/hermes-prepared", "add", "-f", "-m", "broken", feed.target)
        git(feed.producer, "push", str(feed.fork), "refs/notes/hermes-prepared")
    else:
        if fault == "wrong-sha":
            feed.note["candidate_sha"] = feed.base
        if fault == "failed-gate":
            feed.note["validation"]["desktop_tests"] = "fail"
        if fault == "bad-ancestor":
            feed.note["custom_sha"] = "f" * 40
        git(feed.producer, "notes", "--ref=refs/notes/hermes-prepared", "add", "-f", "-m",
            json.dumps(feed.note), feed.target)
        git(feed.producer, "push", str(feed.fork), "refs/notes/hermes-prepared")
    with pytest.raises(PreparationRequired):
        prepared.admit_target(feed.root, {}, prepared.policy(feed.root))
    assert git(feed.root, "rev-parse", "HEAD") == feed.base
    assert git(feed.root, "status", "--porcelain") == ""


@pytest.mark.parametrize("fault", ["dirty", "diverged", "local-ahead", "detached", "wrong-branch"])
def test_unprepared_checkout_is_kept_intact(feed, fault):
    def local_ahead():
        git(feed.root, "merge", "--ff-only", feed.target)
        commit(feed.root, "ahead of prepared feed")
    {"dirty": lambda: (feed.root / "local-work").write_text("keep me"),
     "diverged": lambda: commit(feed.root, "private unprepared change"),
     "local-ahead": local_ahead, "detached": lambda: git(feed.root, "checkout", "--detach"),
     "wrong-branch": lambda: git(feed.root, "checkout", "-qb", "custom/other")}[fault]()
    before = git(feed.root, "rev-parse", "HEAD"), git(feed.root, "status", "--porcelain")
    with pytest.raises(PreparationRequired):
        prepared.admit_target(feed.root, {}, prepared.policy(feed.root))
    assert (git(feed.root, "rev-parse", "HEAD"), git(feed.root, "status", "--porcelain")) == before


def test_named_profile_cannot_escape_physical_install_authority(feed, monkeypatch):
    profile = feed.home / "profiles" / "other"
    profile.mkdir(parents=True)
    path = profile / "config.yaml"
    path.write_text("update: {channel: stable}\nupdates: {prepared_channels: {}}\n")
    before = path.read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(profile))
    settings = prepared.policy(feed.root)
    assert settings["branch"] == "prepared/fixture"
    args = argparse.Namespace(branch=None, channel=None, set_channel=None, switch_branch=False)
    prepared.prepare_args(feed.root, args)
    assert args.branch == "prepared/fixture"
    assert path.read_bytes() == before
    from hermes_cli.update_channel import set_install_channel
    with pytest.raises(PreparationRequired):
        set_install_channel("stable", feed.root)
    assert path.read_bytes() == before


@pytest.mark.parametrize("override", [{"branch": "main"}, {"channel": "stable"},
                                     {"set_channel": "canary"}, {"switch_branch": True}])
def test_explicit_override_cannot_update_unprepared_main(feed, monkeypatch, override):
    from hermes_cli import update_cmd_common
    monkeypatch.setattr(update_cmd_common, "_record_stop", lambda *_a, **_kw: None)
    args = argparse.Namespace(branch=None, channel=None, set_channel=None, switch_branch=False)
    vars(args).update(override)
    with pytest.raises(SystemExit) as exc:
        prepared.prepare_args(feed.root, args)
    assert exc.value.code == 2
    assert git(feed.root, "rev-parse", "HEAD") == feed.base


def remote_graph_adapter(feed, monkeypatch):
    """Adapt HTTP to real Git object contents, including the note's fanout tree."""
    from hermes_cli import source_check

    def request(url, *_):
        tail = url.split("/git/", 1)[1]
        if tail.startswith("ref/"):
            return json.dumps({"object": {"sha": git(feed.fork, "rev-parse", "refs/" + tail[4:])}})
        sha = tail.split("/", 1)[1].split("?", 1)[0]
        if tail.startswith("commits/"):
            return json.dumps({"tree": {"sha": git(feed.fork, "rev-parse", f"{sha}^{{tree}}")}})
        if tail.startswith("trees/"):
            entries = []
            for row in git(feed.fork, "ls-tree", "-r", sha).splitlines():
                _mode, kind, blob_and_path = row.split(" ", 2)
                blob, path = blob_and_path.split("\t", 1)
                entries.append({"type": kind, "sha": blob, "path": path})
            return json.dumps({"tree": entries, "truncated": False})
        text = git(feed.fork, "cat-file", "blob", sha)
        return json.dumps({"encoding": "base64", "size": len(text.encode()),
                           "content": base64.b64encode(text.encode()).decode()})
    monkeypatch.setattr(source_check, "_request", request)


def test_read_only_source_check_tracks_only_attested_tip_and_busts_old_cache(feed, monkeypatch):
    from hermes_cli import source_check

    branches = []
    def tip(_repo, branch, *_args, **_kwargs):
        branches.append(branch)
        return git(feed.fork, "rev-parse", "refs/heads/" + branch), False, None
    monkeypatch.setattr(source_check, "_branch_tip", tip)
    monkeypatch.setattr(source_check, "_behind_count", lambda co, target: (
        int(git(feed.producer, "rev-list", "--count", f"{co.head}..{target}")), []))
    remote_graph_adapter(feed, monkeypatch)
    before = git(feed.root, "rev-parse", "HEAD")
    initial = source_check.check_for_updates(install_root=feed.root, home=feed.home)
    assert initial["targetSha"] == feed.target and initial["updateAvailable"] is True
    assert initial["branch"] == "prepared/fixture"
    assert source_check.check_for_updates(install_root=feed.root, home=feed.home)["targetSha"] == feed.target
    assert branches == ["prepared/fixture"]
    next_target = commit(feed.producer, "next verified upstream change")
    feed.note.update(candidate_sha=next_target, upstream_sha=next_target, previous_ready_sha=feed.target)
    publish(feed)
    import time
    previous_time = time.time()
    monkeypatch.setattr(time, "time", lambda: previous_time + 121)
    current = source_check.check_for_updates(install_root=feed.root, home=feed.home)
    assert current["targetSha"] == next_target
    assert branches == ["prepared/fixture", "prepared/fixture"]
    assert git(feed.root, "rev-parse", "HEAD") == before
    assert git(feed.root, "status", "--porcelain") == ""


def test_missing_prepared_tip_never_falls_back_to_main(feed, monkeypatch):
    from hermes_cli import source_check

    monkeypatch.setattr(source_check, "_branch_tip", lambda _repo, branch, *_a, **_kw: (
        None, True, None) if branch == "prepared/fixture" else pytest.fail("unprepared main was checked"))
    result = source_check.check_for_updates(install_root=feed.root, home=feed.home)
    assert result["error"] == "preparation-required"
    assert result["updateAvailable"] is False
    assert git(feed.root, "rev-parse", "HEAD") == feed.base


def test_remote_note_protocol_reads_actual_git_note(feed, monkeypatch):
    remote_graph_adapter(feed, monkeypatch)
    assert remote_note("mattcreech/hermes-agent", "refs/notes/hermes-prepared", feed.target) == feed.note


def test_deleted_owner_policy_is_refused_under_a_different_profile(feed, monkeypatch):
    (feed.home / "config.yaml").write_text("updates: {prepared_channels: {}}\n")
    profile = feed.home / "profiles" / "other"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    with pytest.raises(PreparationRequired, match="removed or disabled"):
        prepared.policy(feed.root)


def test_runtime_code_changes_require_new_validation_but_memory_data_does_not(feed):
    from hermes_cli.update_prepared_inputs import canonical_digest, plugin_digest, runtime_inputs

    plugin = feed.home / "plugins" / "fixture"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("version = 1\n")
    config_path = feed.home / "config.yaml"
    config = json.loads(config_path.read_text())
    config["plugins"] = {"enabled": ["fixture"]}
    config_path.write_text(json.dumps(config))
    first = runtime_inputs(feed.root, feed.home)
    feed.note["input_digest"] = prepared_digest = canonical_digest(first)
    publish(feed)
    prepared.admit_target(feed.root, {}, prepared.policy(feed.root))
    (plugin / "data").mkdir()
    (plugin / "data" / "memory.db").write_bytes(b"live conversation state")
    (plugin / ".env").write_text("PRIVATE_FIXTURE_KEY=not-a-real-key\n")
    assert runtime_inputs(feed.root, feed.home) == first
    assert first["plugins"][0]["digest"] == plugin_digest(plugin)
    (plugin / "__init__.py").write_text("version = 2\n")
    with pytest.raises(PreparationRequired, match="changed since preparation"):
        prepared.admit_target(feed.root, {}, prepared.policy(feed.root))
    assert prepared_digest == feed.note["input_digest"]


def test_per_install_runtime_proof_never_borrows_another_installation(feed):
    from hermes_cli.update_channel import install_id
    feed.note["input_digests"] = {"0" * 16: feed.note["input_digest"]}
    publish(feed)
    with pytest.raises(PreparationRequired, match="no runtime-validation proof"):
        prepared.admit_target(feed.root, {}, prepared.policy(feed.root))
    feed.note["input_digests"][install_id(feed.root)] = feed.note["input_digest"]
    publish(feed)
    assert prepared.admit_target(feed.root, {}, prepared.policy(feed.root)) == feed.target


def test_native_mnemosyne_settings_are_hashed_without_credentials(feed):
    from hermes_cli.update_prepared_inputs import runtime_inputs

    directory = feed.home / "mnemosyne"
    directory.mkdir()
    (directory / "config.yaml").write_text(
        "host_llm_enabled: true\nhost_llm_model: scoped-fixture\napi_key: fixture-secret\n")
    snapshot = runtime_inputs(feed.root, feed.home)
    assert snapshot["profiles"][0]["mnemosyne_config"] == {
        "host_llm_enabled": True, "host_llm_model": "scoped-fixture"}
    assert "fixture-secret" not in json.dumps(snapshot)
    with pytest.raises(PreparationRequired, match="changed since preparation"):
        prepared.admit_target(feed.root, {}, prepared.policy(feed.root))


def test_consumption_cannot_invoke_inline_inference_repair(feed):
    from hermes_cli import update_managed as managed

    path = feed.home / "config.yaml"
    config = json.loads(path.read_text())
    config["updates"]["maintained_publication"] = {
        "enabled": True, "branch": "custom/fixture", "url": feed.settings["url"], "auto_repair": True}
    path.write_text(json.dumps(config))
    state = managed.begin(feed.root)
    assert state["error"] is None
    assert state["settings"]["auto_repair"] is False
    assert json.loads(path.read_text())["updates"]["maintained_publication"]["auto_repair"] is True


def test_bundled_overlay_digest_does_not_pin_old_tracked_source(feed):
    from hermes_cli.update_prepared_inputs import plugin_digest, runtime_inputs

    directory = feed.root / "plugins" / "mixed"
    directory.mkdir(parents=True)
    tracked, overlay = directory / "__init__.py", directory / "custom.py"
    tracked.write_text("upstream_version = 1\n")
    overlay.write_text("customization = 1\n")
    (feed.root / ".git" / "info" / "exclude").write_text("plugins/mixed/custom.py\n")
    git(feed.root, "add", "plugins/mixed/__init__.py")
    git(feed.root, "commit", "-qm", "bundled source")
    path = feed.home / "config.yaml"
    config = json.loads(path.read_text())
    config["plugins"] = {"enabled": ["mixed"]}
    path.write_text(json.dumps(config))
    first = runtime_inputs(feed.root, feed.home)
    row = first["plugins"][0]
    assert row["scope"] == "untracked-bundled"
    assert row["overlay_paths"] == ["custom.py"]
    assert row["digest"] == plugin_digest(directory, ["custom.py"])
    tracked.write_text("upstream_version = 2\n")
    git(feed.root, "add", "plugins/mixed/__init__.py")
    git(feed.root, "commit", "-qm", "new verified bundled source")
    assert runtime_inputs(feed.root, feed.home) == first
    overlay.write_text("customization = 2\n")
    assert runtime_inputs(feed.root, feed.home) != first
