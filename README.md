# ByzHeist

Can an AI rob a Byzantine-fault-tolerant bank? A Red agent (or a judge, by voice) controls one replica of a PBFT bank and tries to steal money or degrade it without getting caught. A Blue watchdog agent watches visual telemetry, convicts the malicious replica with evidence, and quarantines and replaces it. A referee checks that money is conserved and honest replicas agree.

## Quick start

```bash
# Python 3.11+ required (google-genai >= 3.2.0)
uv venv .venv --python 3.12
uv pip install --python .venv -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY for live Gemini 4 + Decisions API calls
.venv/Scripts/pytest -q
```

## What was built at the hackathon

Everything in this repository—PBFT protocol, cryptographic message signing and verifiable evidence, Byzantine fault injection, telemetry image rendering, Gemini 4 Red/Blue agents, FastAPI/SVG arena UI, and evaluation harness—was written from scratch on **November 21, 2026** at the **Build with Gemini 4 NYC Hackathon** (Cerebral Valley x Google DeepMind).
