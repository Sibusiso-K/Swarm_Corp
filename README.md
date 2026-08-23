# swarm-lab

A Coder/Reviewer swarm on Groq's free tier, with Claude Code as the manual
escalation lane. See [`CLAUDE.md`](CLAUDE.md) for the constraints this
project runs under — read it before changing `agent_swarm.py`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env      # then paste your Groq key into GROQ_API_KEY
python verify_key.py        # confirms the key works and models are live
```

Get a free key at https://console.groq.com/keys.

## Run

```bash
python agent_swarm.py "add a /health endpoint with a test"
```

The Coder drafts a solution, the Reviewer (a different model family, by
design — see `CLAUDE.md`) audits it, and they go back and forth up to
`MAX_ROUNDS` times. Every run's transcript lands in `swarm_output/`
regardless of outcome.

If it doesn't get approved in time, that's not a bug — it's the signal to
take the same task to Claude Code instead.

## Layout

- `agent_swarm.py` — the orchestration loop
- `verify_key.py` — run first, and any time something breaks
- `agents/*.md` — Coder/Reviewer/Orchestrator personas
- `swarm_output/` — per-run transcripts (gitignored)
