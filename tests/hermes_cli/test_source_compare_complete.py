"""Compare decisions require a complete HTTP JSON body, even above 2 MiB."""
import io
import json

import pytest

from hermes_cli import github_api, source_check


@pytest.mark.parametrize("invalid_suffix", [False, True])
def test_compare_reads_complete_response(monkeypatch, invalid_suffix):
    limit = 2 * 1024 * 1024
    comparison = {"status": "ahead", "ahead_by": 2666, "behind_by": 0,
                  "commits": [], "files": [{"patch": "x" * limit}]}
    body = json.dumps(comparison).encode()
    if invalid_suffix:
        # A valid JSON prefix must not hide invalid bytes beyond the old cap.
        prefix = json.dumps({"status": "ahead", "ahead_by": 2666}).encode()
        body = prefix + b" " * (limit - len(prefix)) + b"invalid"
    monkeypatch.setattr(github_api, "github_token", lambda: None)
    monkeypatch.setattr(source_check.urllib.request, "urlopen",
                        lambda request, timeout: io.BytesIO(body))

    result = source_check._github_compare("a" * 40, "b" * 40)

    assert result == (None if invalid_suffix else comparison)
