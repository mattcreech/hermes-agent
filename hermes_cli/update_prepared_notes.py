"""Validation attestations published atomically with a prepared source branch."""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

VALIDATION_STAGES = (
    "python_contracts", "dependency_environment", "required_capabilities",
    "desktop_tests", "desktop_types", "macos_build", "repository_checks",
)


class PreparationRequired(ValueError):
    """The prepared feed has no admissible verified candidate."""


def required_gates(platform: str) -> tuple[str, ...]:
    """Linux backend validation does not depend on a separate Darwin build host."""
    return tuple(stage for stage in VALIDATION_STAGES if platform != "linux" or stage != "macos_build")


def validate_gates(validation: object, platform: str) -> None:
    """Require host gates and preserve an explicitly unrequested Linux macOS build."""
    if (not isinstance(validation, dict)
            or any(validation.get(stage) != "pass" for stage in required_gates(platform))
            or any(value != "pass" and not (platform == "linux" and stage == "macos_build"
                                           and value == "not_requested")
                   for stage, value in validation.items())):
        raise PreparationRequired("Prepared candidate has not passed every required validation gate")


def validate_note(text: str, candidate_sha: str) -> dict:
    """An attestation admits only its exact commit, with every required gate passed."""
    from hermes_platform.host.facts import os_family

    try:
        note = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise PreparationRequired("Prepared validation note is unreadable") from exc
    if not isinstance(note, dict) or type(note.get("schema_version")) is not int or note["schema_version"] != 1:
        raise PreparationRequired("Prepared validation note has an unsupported schema")
    for key in ("candidate_sha", "upstream_sha", "custom_sha"):
        if not isinstance(note.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}", note[key]):
            raise PreparationRequired(f"Prepared validation note has an invalid {key}")
    previous = note.get("previous_ready_sha")
    if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{40}", previous)):
        raise PreparationRequired("Prepared validation note has an invalid previous_ready_sha")
    for key in ("input_digest", "contracts_digest"):
        if not isinstance(note.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", note[key]):
            raise PreparationRequired(f"Prepared validation note has an invalid {key}")
    digests = note.get("input_digests")
    if digests is not None:
        if (not isinstance(digests, dict) or not digests
                or any(not re.fullmatch(r"[0-9a-f]{16}", key) for key in digests)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in digests.values())):
            raise PreparationRequired("Prepared validation note has invalid per-install input digests")
    elif not isinstance(note.get("owner_install_id"), str) or not re.fullmatch(r"[0-9a-f]{16}", note["owner_install_id"]):
        raise PreparationRequired("Prepared validation note does not bind its runtime proof to an installation")
    if note["candidate_sha"] != candidate_sha:
        raise PreparationRequired("Prepared validation note names a different source commit")
    validate_gates(note.get("validation"), os_family())
    if not isinstance(note.get("validated_at"), str) or not note["validated_at"]:
        raise PreparationRequired("Prepared validation note has no validation time")
    return note


def remote_note(repository: str, notes_ref: str, candidate_sha: str) -> dict:
    """Read GitHub's Git objects without fetching or writing the live checkout."""
    from hermes_cli.source_check import _request

    base = f"https://api.github.com/repos/{repository}/git"
    try:
        ref = json.loads(_request(f"{base}/ref/{quote(notes_ref.removeprefix('refs/'), safe='/')}"))
        commit = json.loads(_request(f"{base}/commits/{ref['object']['sha']}"))
        tree = json.loads(_request(f"{base}/trees/{commit['tree']['sha']}?recursive=1"))
        if tree.get("truncated"):
            raise PreparationRequired("Prepared validation-note tree is incomplete")
        matches = [entry for entry in tree["tree"]
                   if entry.get("type") == "blob" and entry.get("path", "").replace("/", "") == candidate_sha]
        if len(matches) != 1:
            raise PreparationRequired("Prepared branch has no validation note for its exact commit")
        blob = json.loads(_request(f"{base}/blobs/{matches[0]['sha']}"))
        if blob.get("encoding") != "base64" or not isinstance(blob.get("size"), int) or blob["size"] > 65536:
            raise PreparationRequired("Prepared validation note exceeds the admitted format")
        text = base64.b64decode(blob["content"]).decode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        if isinstance(exc, PreparationRequired):
            raise
        raise PreparationRequired("Prepared validation-note objects are unreadable") from exc
    return validate_note(text, candidate_sha)
