"""Stretch S1 tests: `/red/status` endpoint, `X-Arena-Access-Code` header auth, and Managed Agent dry run."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from byzheist import config
from byzheist.server import create_app
from scripts.red_campaign import FakeInteractionClient, run_red_campaign


def test_red_status_endpoint_and_managed_agent_campaign_dry_run(tmp_path: Path) -> None:
    app = create_app(
        use_fake_gemini=True,
        load_rate_hz=2.0,
        blue_interval_sec=2.0,
        red_adapt_interval_sec=30.0,
        access_code="tunnel-secret-99",
    )
    with TestClient(app) as http_client:
        # 1. Verify `X-Arena-Access-Code` header unlocks tunneled `/red/status`
        tunnel_hdrs = {
            "Cf-Connecting-IP": "203.0.113.77",
            "X-Arena-Access-Code": "tunnel-secret-99",
        }
        st_resp = http_client.get("/red/status", headers=tunnel_hdrs)
        assert st_resp.status_code == 200
        st = st_resp.json()
        assert "active_mode" in st
        assert "flagged_by_blue" in st
        assert "throughput_impact_pct" in st

        # 2. Dry-run 3-round Managed Agent campaign reusing `environment_id` across turns
        fake_agent = FakeInteractionClient(http_client=http_client)
        out_playbook = tmp_path / "red_playbook.md"
        result = run_red_campaign(
            public_url="https://example-arena.trycloudflare.com",
            access_code="tunnel-secret-99",
            local_arena_url="http://127.0.0.1:59999",
            rounds=3,
            observe_seconds=5,
            output_path=out_playbook,
            client=fake_agent,
        )

        assert out_playbook.is_file()
        assert "Playbook" in out_playbook.read_text(encoding="utf-8")
        assert len(fake_agent.calls) == 3
        assert fake_agent.calls[0]["agent"] == config.MODEL_IDS["antigravity_agent"]
        assert fake_agent.calls[0]["agent_config"]["model"] == config.MODEL_IDS["frontier_fast"]
        assert fake_agent.calls[0]["agent_config"]["max_total_tokens"] == 150_000
        # Turns 2 and 3 reuse the returned `environment_id` and `previous_interaction_id`
        assert fake_agent.calls[1]["environment"] == "env-fake-byzheist-01"
        assert fake_agent.calls[1]["previous_interaction_id"] == "inter-fake-1"
        assert fake_agent.calls[2]["environment"] == "env-fake-byzheist-01"
        assert fake_agent.calls[2]["previous_interaction_id"] == "inter-fake-2"
        assert result["credential_mode"] == "credentials_api_allowlist"
