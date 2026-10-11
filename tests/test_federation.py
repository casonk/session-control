from __future__ import annotations

from dataclasses import replace

from session_control.federation import build_inventory, load_inventories, store_inventory
from session_control.scanner import SessionScanner
from session_control.web import create_app
from tests.helpers import seed_codex


def test_inventory_excludes_private_session_fields(app_config):
    seed_codex(app_config.codex_root)

    inventory = build_inventory(
        SessionScanner(app_config).scan(), node_id="air", node_label="MacBook Air"
    )

    assert inventory["node"] == {"id": "air", "label": "MacBook Air"}
    session = inventory["sessions"][0]
    assert session["workspace_label"] == "example-project"
    assert "preview" not in session
    assert "primary_path" not in session
    assert "delete_targets" not in session
    assert "resume_command" not in session
    assert "/workspace" not in str(inventory)


def test_store_and_load_inventory_rejects_path_mismatch(app_config, tmp_path):
    seed_codex(app_config.codex_root)
    inventory = build_inventory(SessionScanner(app_config).scan(), node_id="air", node_label="Air")
    store = tmp_path / "federation"

    stored = store_inventory(store, inventory, expected_node_id="air")
    loaded, errors = load_inventories(store)

    assert stored == loaded[0]
    assert errors == []
    assert (store / "air.json").stat().st_mode & 0o777 == 0o600


def test_federation_ingest_requires_node_token_and_renders_remote(app_config, tmp_path):
    seed_codex(app_config.codex_root)
    config = replace(
        app_config,
        federation_node_id="desk",
        federation_node_label="Server",
        federation_store_dir=tmp_path / "federation",
        federation_tokens={"air": "test-token"},
    )
    remote = build_inventory(SessionScanner(config).scan(), node_id="air", node_label="MacBook Air")
    client = create_app(config).test_client()

    denied = client.post("/api/federation/v1/inventories/air", json=remote)
    accepted = client.post(
        "/api/federation/v1/inventories/air",
        json=remote,
        headers={"Authorization": "Bearer test-token"},
    )
    page = client.get("/federation")

    assert denied.status_code == 401
    assert accepted.status_code == 201
    assert accepted.json["node"] == {"id": "air", "label": "MacBook Air"}
    assert page.status_code == 200
    assert "MacBook Air" in page.get_data(as_text=True)
    assert "Open" not in page.get_data(as_text=True)


def test_federation_export_endpoint_excludes_private_fields(app_config):
    seed_codex(app_config.codex_root)
    config = replace(app_config, federation_node_id="air", federation_node_label="MacBook Air")

    response = create_app(config).test_client().get("/api/federation/v1/inventory")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["node"] == {"id": "air", "label": "MacBook Air"}
    assert "/workspace" not in str(payload)
    assert "Fix the downloader tests" not in str(payload)
    assert set(payload["sessions"][0]) == {
        "id",
        "provider",
        "title",
        "workspace_label",
        "created_at",
        "updated_at",
        "message_count",
        "size_bytes",
        "active",
        "model",
    }
