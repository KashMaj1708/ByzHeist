# Instructions for the coding agent

You are building **ByzHeist** for a hackathon that ends at **5:00 PM today (Nov 21, 2026)**. The full plan is in `PLAN.md`. Follow it phase by phase.

## Loop for every phase
1. Read the phase in `PLAN.md`.
2. Build it. Keep modules small and simple.
3. Run the phase tests plus all earlier tests: `pytest -q`.
4. Write `reports/PHASE_N.md` from the template at the end of `PLAN.md`. Be honest about failures and cuts.
5. `git add -A && git commit -m "Phase N: <summary>" && git push`
6. **Stop.** Say "Phase N done, report at reports/PHASE_N.md" and wait for **"GREENLIGHT N+1"**. Do not start the next phase on your own.

## Rules
- Speed over polish. If it works and nothing is seriously broken, it is done. No refactors, no speculative abstractions, no extra features outside the phase.
- If you are 15+ minutes past a phase's target time, cut scope within the phase and record the cut in the report.
- Never commit secrets. The key lives in `.env` (`GEMINI_API_KEY`), which is gitignored. Never print the key.
- Must run on Windows: asyncio only, no fork, no uvloop, paths via `pathlib`.
- Gemini API: use the official Gemini API skill / docs server to confirm call shapes. Use model IDs only from `byzheist/config.py`. Do not pass temperature, top_p, top_k or thinking_budget to Gemini 4 models; do not use the `minimal` thinking level on Neon or Hydrogen. Use structured outputs / Decisions API questions, not "reply in JSON" prompts.
- Respect the API call cap in config. The temporary account can be disqualified for abnormal usage.
- Tests must run offline with `FakeGemini`; live tests are marked `@pytest.mark.live` and skipped without a key.
- Do not add Streamlit. The UI is FastAPI + a single static HTML page.
