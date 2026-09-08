from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

import research_lab.app as lab


def test_workspace_run_and_cleanup(tmp_path: Path) -> None:
    lab.DATA_DIR = tmp_path
    lab.WORKSPACES_DIR = tmp_path / "workspaces"
    lab.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    lab.TOKEN = "test-token"
    lab.MAX_MEMORY_MB = 1024

    client = TestClient(lab.app)
    headers = {"Authorization": "Bearer test-token"}

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    created = client.post(
        "/api/workspaces",
        headers=headers,
        json={"name": "Theory test", "description": "temporary", "source_project": "pytest"},
    )
    assert created.status_code == 200, created.text
    workspace_id = created.json()["id"]

    run = client.post(
        f"/api/workspaces/{workspace_id}/runs",
        headers=headers,
        json={
            "title": "smoke",
            "timeout_seconds": 20,
            "code": "from pathlib import Path\nprint(2 + 3)\nPath('artifact.txt').write_text('ok')\n",
        },
    )
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["status"] == "success", payload
    assert "5" in payload["stdout"]
    assert any(a["path"] == "artifact.txt" for a in payload["artifacts"])

    runs = client.get(f"/api/workspaces/{workspace_id}/runs", headers=headers)
    assert runs.status_code == 200
    assert len(runs.json()) == 1

    run_id = payload["id"]
    deleted_run = client.delete(
        f"/api/workspaces/{workspace_id}/runs/{run_id}?confirm=true",
        headers=headers,
    )
    assert deleted_run.status_code == 200

    deleted_ws = client.delete(f"/api/workspaces/{workspace_id}?confirm=true", headers=headers)
    assert deleted_ws.status_code == 200
    assert not (lab.WORKSPACES_DIR / workspace_id).exists()


def test_auth_required(tmp_path: Path) -> None:
    lab.DATA_DIR = tmp_path
    lab.WORKSPACES_DIR = tmp_path / "workspaces"
    lab.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    lab.TOKEN = "secret"
    client = TestClient(lab.app)
    assert client.get("/api/workspaces").status_code == 401
