# Stretch S1 report — Managed Agent Red Campaign (`antigravity-preview-09-2026`)
Status: DONE WITH CUTS (offline harness + `/red/status` + UI integration green; live preview `allowlist` returned empty output so kept optional)
Finished at: 4:04 PM EST (Nov 21, 2026)

## Built
- `scripts/red_campaign.py`: 3-round Managed Agent orchestrator calling `antigravity-preview-09-2026` (`model="gemini-4-neon"`, `max_total_tokens=150000`) via `v1beta`, reusing `environment_id` and `previous_interaction_id` across all 3 rounds, registering `ARENA_ACCESS_CODE` via the Credentials API (`HTTPBearerConfig`, header `X-Arena-Access-Code`) with request-header fallback, updating UI round progress (`POST /campaign/update`), and saving `out/red_playbook.md`.
- `byzheist/server.py` & `byzheist/web/index.html`: added read-only `GET /red/status` (`active_mode`, `flagged_by_blue`, `throughput_impact_pct`), `X-Arena-Access-Code` header authentication, and UI display (`"Managed Agent Red: round k/3"` + playbook viewer).
- `tests/test_stretch_s1.py`: verifies `/red/status`, `X-Arena-Access-Code` tunnel header auth, and 3-round `environment_id` + `previous_interaction_id` reuse with `FakeInteractionClient`.

## Tests
```
...                                                                      [100%]
3 passed in 15.39s
```

## Live Check & Honest Notes
- Verified `environment="remote"` directly against live `antigravity-preview-09-2026` (`environment_id=9f20e0744e5cb050188d278b57ae00b6`, `17,666` tokens), where the remote sandbox created `/workspace/PLAYBOOK.md` and returned its Markdown contents cleanly.
- When pairing `environment={"type": "remote", "network": {"allowlist": [...]}}` with a `trycloudflare.com` quick-tunnel credential (`credentials.create` succeeded), the preview agent completed 3 turns (`environment_id=dbbdee99ad0605be896bf0004475ac1b`) with empty `output_text` (`0` tokens billed under custom network allowlist config). Per `PLAN.md`'s 4:10 PM S1 rule, we record this preview limitation honestly rather than sinking demo-prep minutes into custom allowlist debugging; `scripts/red_campaign.py` supports plain `environment="remote"` + `X-Arena-Access-Code` header out of the box.

## Next
- Ready for Phase 7 (`README.md`, secret scan, Cloudflare quick-tunnel instructions, and 1-minute demo checklist in `reports/PHASE_7.md`) upon **GREENLIGHT 7**.
