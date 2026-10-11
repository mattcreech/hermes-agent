"""Complete only conflict resolutions Git has already learned and staged."""
from __future__ import annotations


def finish_recorded_resolution(git_cmd, target: str) -> bool:
    from hermes_cli import update_cmd as update

    def run(args):
        return update._git_run(git_cmd, args)
    if run(["config", "--bool", "--get", "rerere.autoupdate"]).stdout.strip() != "true":
        return False
    merge = run(["rev-parse", "-q", "--verify", "MERGE_HEAD"])
    expected = run(["rev-parse", "--verify", f"{target}^{{commit}}"])
    if merge.returncode or expected.returncode or merge.stdout.strip() != expected.stdout.strip():
        return False
    unmerged = run(["diff", "--name-only", "--diff-filter=U"])
    if unmerged.returncode or unmerged.stdout.strip():
        return False
    # Only rerere's staged resolution is eligible. No automatic add/ours/theirs.
    if run(["diff", "--quiet"]).returncode or run(["diff", "--cached", "--check"]).returncode:
        return False
    if run(["commit", "--no-edit"]).returncode:
        return False
    print("  ✓ Reused a recorded conflict resolution; behavior verification follows.")
    return True
