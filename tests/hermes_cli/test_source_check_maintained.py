"""Maintained-branch target contracts, separate from the upstream checker suite."""
import json
from uuid import uuid4

import pytest

from tests.hermes_cli.test_source_check import MAIN_CHANNEL, installation as installation, source_channel  # noqa: PLC0414 — imported pytest fixture

pytestmark = pytest.mark.real_release_channels


@pytest.mark.parametrize("main_record_published", [False, True])
def test_update_in_place_checks_main_without_leaving_custom_branch(installation, main_record_published):
    from hermes_cli.source_check import check_for_updates

    _root, linked, home, _base, _head, responses, requests, git = installation
    git("remote", "set-url", "origin", "https://github.com/NousResearch/hermes-agent.git")
    git("commit", "--allow-empty", "-m", "custom patch", cwd=linked)
    custom_head = git("rev-parse", "HEAD", cwd=linked)
    (home / "config.yaml").write_text("updates: {parked_branch_strategy: update_in_place}\n")
    if not main_record_published:
        responses[MAIN_CHANNEL] = (404, {})
    target = "a" * 40
    main_path = "/repos/NousResearch/hermes-agent/commits/main"
    responses[main_path] = (200, target)
    responses[f"/repos/NousResearch/hermes-agent/compare/{custom_head}...{target}"] = (
        200, {"ahead_by": 3, "commits": []})

    # Official origins now default to stable. This contract explicitly exercises
    # the maintained installation's chosen main subscription, not that default.
    status = check_for_updates(install_root=linked, home=home, channel="main", force=True)

    assert status.get("branch") == "main", status
    assert "error" not in status, status
    assert status["currentBranch"] == "feature/gui"
    assert status["behind"] == 3
    assert status["updateAvailable"] is True
    assert main_path in requests
    assert MAIN_CHANNEL not in requests
    assert "/repos/NousResearch/hermes-agent/commits/feature%2Fgui" not in requests
    assert git("branch", "--show-current", cwd=linked) == "feature/gui"
    assert git("rev-parse", "HEAD", cwd=linked) == custom_head
    assert git("status", "--porcelain", cwd=linked) == ""


@pytest.mark.parametrize("selection", ["explicit", "configured"])
def test_update_in_place_preserves_explicit_branch_selection(installation, selection):
    from hermes_cli.source_check import check_for_updates

    _root, linked, home, _base, head, responses, _requests, git = installation
    (home / "config.yaml").write_text("updates: {parked_branch_strategy: update_in_place}\n")
    branch_file = home / "desktop-update.json"
    branch_file.write_text(json.dumps({"branch": "desktop-choice"}))
    expected = "explicit-choice" if selection == "explicit" else "desktop-choice"
    responses[f"/repos/fixture/fork/commits/{expected}"] = (200, head)

    status = check_for_updates(
        install_root=linked, home=home, force=True, branch_config_path=branch_file,
        branch=expected if selection == "explicit" else None,
    )

    assert "error" not in status, status
    assert status["branch"] == expected
    assert status["behind"] == 0
    assert json.loads(branch_file.read_text()) == {"branch": "desktop-choice"}
    assert git("branch", "--show-current", cwd=linked) == "feature/gui"


def test_update_in_place_policy_change_invalidates_branch_check_cache(installation):
    from hermes_cli.source_check import check_for_updates

    _root, linked, home, _base, head, responses, requests, git = installation
    responses["/repos/fixture/fork/commits/feature%2Fgui"] = (200, head)
    responses["/repos/fixture/fork/commits/main"] = (200, head)

    def check():
        return check_for_updates(install_root=linked, home=home)

    assert check()["branch"] == "feature/gui"
    (home / "config.yaml").write_text("updates: {parked_branch_strategy: update_in_place}\n")
    assert check()["branch"] == "main"
    (home / "config.yaml").write_text("updates: {parked_branch_strategy: switch}\n")
    assert check()["branch"] == "feature/gui"
    assert requests.count("/repos/fixture/fork/commits/main") == 1
    assert git("branch", "--show-current", cwd=linked) == "feature/gui"


@pytest.mark.parametrize("selection", ["explicit", "configured", "current", "detached"])
def test_dynamic_source_channel_preserves_branch_precedence(installation, selection):
    from hermes_cli.source_check import check_for_updates

    _root, linked, home, _base, head, responses, requests, git = installation
    (home / "config.yaml").write_text("updates: {parked_branch_strategy: update_in_place}\n")
    name = "branch-" + uuid4().hex[:12]
    channel_path = f"/releases/channels/{name}.json"
    responses[channel_path] = (200, source_channel(name, "fixture/fork", "channel-default"))
    branch_file = home / "desktop-update.json"
    if selection in {"explicit", "configured"}:
        branch_file.write_text(json.dumps({"branch": "desktop-choice"}))
    if selection == "detached":
        git("checkout", "--detach", cwd=linked)
    expected = {"explicit": "explicit-choice", "configured": "desktop-choice",
                "current": "feature/gui", "detached": "channel-default"}[selection]
    branch_path = f"/repos/fixture/fork/commits/{expected.replace('/', '%2F')}"
    responses[branch_path] = (200, head)

    status = check_for_updates(install_root=linked, home=home, channel=name,
                              branch="explicit-choice" if selection == "explicit" else None,
                              branch_config_path=branch_file)
    assert status.get("branch") == expected, status
    assert status.get("targetSha") == head, status
    assert status["behind"] == 0
    assert requests == ([] if selection == "explicit" else [channel_path]) + [branch_path]

