"""Diagnostic links cannot expose arbitrary files or another update action."""
import asyncio

import pytest
from fastapi import HTTPException

from hermes_cli.update_managed_diagnostics import diagnostic_details


def test_diagnostics_reads_only_bounded_owner_logs(tmp_path):
    root = tmp_path / "logs/maintained-tests"
    root.mkdir(parents=True)
    log = root / "failure.log"
    log.write_text("x" * (256 * 1024) + "\nFAILED custom behavior")
    private = tmp_path / "private"
    private.write_text("PRIVATE")
    link = root / "link.log"
    link.symlink_to(private)
    receipt = {"update_id": "fixture", "outcome": "failed", "followups": [
        {"step": "managed_publication", "reason": f"tests: diagnostics: {log}"},
        {"step": "invalid", "reason": f"diagnostics: {private}"},
        {"step": "invalid", "reason": f"diagnostics: {link}"}],
        "managed_repair": {"status": "failed", "reason": "could not preserve behavior"}}
    detail = diagnostic_details(tmp_path, receipt)
    assert "FAILED custom behavior" in detail
    assert "PRIVATE" not in detail
    assert "could not preserve behavior" in detail
    assert len(detail) < 140 * 1024


def test_diagnostics_route_requires_exact_action_receipt(monkeypatch, tmp_path):
    from hermes_cli.web_routers import actions

    monkeypatch.setattr(actions, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.update_receipt.read_receipt_for_action", lambda _: None)
    monkeypatch.setattr(actions, "_read_latest_receipt", lambda: {"action_id": "different", "outcome": "success"})
    with pytest.raises(HTTPException) as failure:
        asyncio.run(actions.get_update_details(action_id="requested"))
    assert failure.value.status_code == 404
    response = asyncio.run(actions.get_update_details(action_id=None))
    assert "Outcome: success" in response.body.decode()
    assert response.media_type == "text/plain"
    assert response.headers["cache-control"] == "no-store"
    assert any(route.path == "/api/hermes/update/details" for route in actions.status_router.routes)
