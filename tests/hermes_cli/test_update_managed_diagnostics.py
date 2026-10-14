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


def test_held_preparation_details_are_visible_for_the_exact_http_action(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers import actions

    root = tmp_path / "logs/prepared-updates"
    root.mkdir(parents=True)
    log = root / "request-fixture.log"
    log.write_text("x" * (256 * 1024) + "\nFAILED required memory import\n")
    action = "a" * 32
    receipt = {"update_id": "held-fixture", "action_id": action, "outcome": "failed",
               "preparation_result": {"status": "held", "stage": "required_capabilities",
                                      "reason": "Repair did not preserve required memory",
                                      "requested_upstream_sha": "b" * 40, "log": str(log),
                                      "repair_decision": {"reason": "Frozen checks still failed",
                                                          "summary": "The previous version was kept"}}}
    monkeypatch.setattr(actions, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.update_receipt.read_receipt_for_action",
                        lambda requested: receipt if requested == action else None)
    monkeypatch.setattr(actions, "_read_latest_receipt", lambda: {
        "update_id": "unrelated", "action_id": "c" * 32, "outcome": "success"})
    app = FastAPI()
    app.include_router(actions.status_router)
    with TestClient(app) as client:
        response = client.get("/api/hermes/update/details", params={"action_id": action})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "held-fixture" in response.text and "unrelated" not in response.text
        for value in ("Status: held", "required_capabilities", "Repair did not preserve required memory",
                      "b" * 40, "Frozen checks still failed", "The previous version was kept",
                      "FAILED required memory import"):
            assert value in response.text
        assert len(response.content) < 140 * 1024
        assert client.get("/api/hermes/update/details", params={"action_id": "d" * 32}).status_code == 404


@pytest.mark.parametrize("target", ["outside", "symlink", "nested", "traversal", "other-log-owner"])
def test_preparation_diagnostics_never_follow_remote_or_arbitrary_paths(tmp_path, target):
    root = tmp_path / "logs/prepared-updates"
    root.mkdir(parents=True)
    private = tmp_path / "private"
    private.write_text("NEVER_EXPOSE_OWNER_SECRET")
    local = root / "request-fixture.log"
    local.write_text("allowed local context")
    (root / "link.log").symlink_to(private)
    (root / "nested").mkdir()
    (root / "nested/failure.log").write_text("NEVER_EXPOSE_OWNER_SECRET")
    maintained = tmp_path / "logs/maintained-tests/failure.log"
    maintained.parent.mkdir()
    maintained.write_text("NEVER_EXPOSE_OWNER_SECRET")
    path = {"outside": private, "symlink": root / "link.log", "nested": root / "nested/failure.log",
            "traversal": root / "../prepared-updates/request-fixture.log",
            "other-log-owner": maintained}[target]
    receipt = {"preparation_result": {"status": "held", "reason": "Known preparation failure",
                                     "log": str(path), "details": str(private),
                                     "repair_decision": {"log": str(private)}}}
    details = diagnostic_details(tmp_path, receipt)
    assert "Known preparation failure" in details
    assert "NEVER_EXPOSE_OWNER_SECRET" not in details
    assert "allowed local context" not in details
