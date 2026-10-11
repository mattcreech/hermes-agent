"""Inference proposals cannot deploy until scoped patches pass real behavior gates."""
from pathlib import Path
import subprocess

import pytest

from hermes_cli import update_managed as managed
from hermes_cli import update_managed_repair as repair
from hermes_cli import update_receipt as receipts

pytestmark = pytest.mark.platforms("linux")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    git(root, "init", "-b", "custom/fixture")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / "hermes_cli").mkdir()
    source = root / "hermes_cli/source_check.py"
    source.write_text("answer = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    source.write_text("answer = 2\n")
    git(root, "commit", "-am", "customization")
    sha = git(root, "rev-parse", "HEAD")
    state = {"sha": sha, "settings": {"branch": "custom/fixture", "url": managed.FORK_URL,
                                       "enabled": True, "auto_repair": True}}
    monkeypatch.setattr("pm.testenv.ensure_testenv", lambda *a, **k: Path("/fixture/python"))
    monkeypatch.setattr("hermes_cli.update_cmd._write_fleet_restart_pending_marker", lambda **kw: True)
    receipts.begin_update_receipt()
    yield root, state
    receipts._current.set(None)


PATCH = """diff --git a/hermes_cli/source_check.py b/hermes_cli/source_check.py
--- a/hermes_cli/source_check.py
+++ b/hermes_cli/source_check.py
@@ -1 +1 @@
-answer = 2
+answer = 3
"""


@pytest.mark.parametrize("fault", [None, "scope", "tests", "concurrent", "obligation"])
def test_only_verified_patch_can_advance_clean_checkout(checkout, monkeypatch, fault):
    root, state = checkout
    original = state["sha"]
    calls = []

    def propose(candidate, directory, prompt, log):
        calls.append(prompt)
        if fault == "concurrent":
            (root / "private.txt").write_text("unrelated work")
        return {"patch": PATCH if fault != "scope" else PATCH.replace("source_check.py", "auth.py"),
                "summary": "fixture repair"}

    def verify(candidate, *, interpreter):
        assert candidate != root
        assert interpreter == Path("/fixture/python")
        assert (candidate / "hermes_cli/source_check.py").read_text() == "answer = 3\n"
        if fault == "tests":
            raise RuntimeError("behavior remains broken")

    monkeypatch.setattr(repair, "_propose", propose)
    monkeypatch.setattr(managed, "_tests", verify)
    if fault == "obligation":
        monkeypatch.setattr("hermes_cli.update_cmd._write_fleet_restart_pending_marker", lambda **kw: False)
    result = repair.repair_tests(root, state, RuntimeError("fixture incompatibility"))
    assert len(calls) == 1
    assert git(root, "branch", "--show-current") == "custom/fixture"
    if fault is None:
        assert result == state["sha"] == git(root, "rev-parse", "HEAD")
        assert result != original
        assert git(root, "merge-base", "--is-ancestor", original, result) == ""
        assert (root / "hermes_cli/source_check.py").read_text() == "answer = 3\n"
        assert git(root, "status", "--porcelain") == ""
    else:
        assert result is None
        assert git(root, "rev-parse", "HEAD") == original
        assert (root / "hermes_cli/source_check.py").read_text() == "answer = 2\n"
        if fault == "concurrent":
            assert (root / "private.txt").read_text() == "unrelated work"
    assert len(git(root, "worktree", "list", "--porcelain").split("worktree ")) == 2


@pytest.mark.parametrize("change", ["assertion", "parameter", "skip"])
def test_proposal_cannot_weaken_existing_test(tmp_path, change):
    root = tmp_path / "checkout"
    (root / "tests").mkdir(parents=True)
    name = "tests/test_contract.py"
    before = "import pytest\n@pytest.mark.parametrize('x', [1, 2])\ndef test_behavior(x):\n    assert x > 0\n"
    after = {"assertion": before.replace("assert x > 0", "assert True"),
             "parameter": before.replace("[1, 2]", "[1]"),
             "skip": before.replace("    assert", "    pytest.skip('ignored')\n    assert")}[change]
    (root / name).write_text(before)
    git(root, "init")
    git(root, "add", ".")
    (root / name).write_text(after)
    patch = git(root, "diff") + "\n"
    (root / name).write_text(before)
    with pytest.raises(ValueError):
        repair._validate_patch(root, patch, {name}, {name: before})


@pytest.mark.parametrize("fault", [None, "tests", "missing_resolution"])
def test_isolated_merge_repair_preserves_both_histories(checkout, monkeypatch, fault):
    root, state = checkout
    original = state["sha"]
    base = git(root, "rev-parse", "origin/main")
    git(root, "checkout", "--detach", base)
    (root / "hermes_cli/source_check.py").write_text("answer = 4\n")
    git(root, "commit", "-am", "new upstream")
    target = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", target)
    git(root, "checkout", "custom/fixture")

    def propose(candidate, directory, prompt, log):
        path = candidate / "hermes_cli/source_check.py"
        before = path.read_text()
        assert "<<<<<<<" in before
        # Produce an ordinary patch from the current conflict-marker bytes.
        import difflib
        after = before if fault == "missing_resolution" else "answer = 2 + 4\n"
        patch = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                            fromfile="a/hermes_cli/source_check.py",
                                            tofile="b/hermes_cli/source_check.py"))
        return {"patch": patch, "summary": "preserve upstream and customization"}

    def verify(candidate, *, interpreter):
        assert (candidate / "hermes_cli/source_check.py").read_text() == "answer = 2 + 4\n"
        if fault == "tests":
            raise RuntimeError("behavior does not hold")

    monkeypatch.setattr(repair, "_propose", propose)
    monkeypatch.setattr(managed, "_tests", verify)
    result = repair.repair_tests(root, state, RuntimeError("merge conflict"), merge_target=target)
    if fault is None:
        assert result == git(root, "rev-parse", "HEAD")
        assert git(root, "merge-base", "--is-ancestor", original, result) == ""
        assert git(root, "merge-base", "--is-ancestor", target, result) == ""
    else:
        assert result is None
        assert original == git(root, "rev-parse", "HEAD")
        assert (root / "hermes_cli/source_check.py").read_text() == "answer = 2\n"
    assert git(root, "status", "--porcelain") == ""


def test_failed_attempt_is_not_repeated_during_same_update(checkout, monkeypatch):
    root, state = checkout
    calls = []
    monkeypatch.setattr(repair, "_propose", lambda *args: calls.append(args) or {"patch": "", "summary": "declined"})
    assert repair.repair_tests(root, state, RuntimeError("fixture incompatibility")) is None
    assert repair.repair_tests(root, state, RuntimeError("same update incompatibility")) is None
    assert len(calls) == 1


def test_source_context_includes_failing_test_and_imported_code(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "hermes_cli").mkdir()
    (tmp_path / "tests/test_fixture.py").write_text("from hermes_cli.source_check import check\n")
    (tmp_path / "hermes_cli/source_check.py").write_text("def check(): return 42\n")
    permitted = {"tests/test_fixture.py", "hermes_cli/source_check.py"}
    text = repair._source_context(tmp_path, permitted, "FAILED tests/test_fixture.py::test_case", [])
    assert "def check(): return 42" in text
    assert "from hermes_cli.source_check import check" in text
    assert len(text) < 160000


def test_git_recounts_inaccurate_inference_hunk_lengths(tmp_path):
    root = tmp_path / "checkout"
    (root / "hermes_cli").mkdir(parents=True)
    name = "hermes_cli/source_check.py"
    (root / name).write_text("answer = 2\n")
    git(root, "init")
    git(root, "add", ".")
    patch = PATCH.replace("@@ -1 +1 @@", "@@ -1,7 +1,7 @@")
    assert repair._validate_patch(root, patch, {name}, {name: "answer = 2\n"}) == [name]
    assert (root / name).read_text() == "answer = 3\n"
