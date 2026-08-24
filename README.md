# Swarm_Corp

A 6-role coding swarm — Planner → Tester → Coder ⇄ Sandbox ⇄ Security/Reviewer
→ Arbiter — running entirely on free LLM provider tiers, with Claude Code as
the manual escalation lane. See [`CLAUDE.md`](CLAUDE.md) for the constraints
this project runs under — read it before changing `swarm_corp.py`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
copy .env.example .env      # then paste your provider keys in
python verify_key.py        # confirms keys work and models actually serve
```

At minimum you need a free Groq key (https://console.groq.com/keys).
Cerebras, NVIDIA NIM, and Google AI Studio are optional — add their keys to
widen the model pool and unlock providers with much higher free-tier TPM.
See `.env.example` for links to each.

## Run

Double-click **Start Swarm_Corp** on the Desktop, or from a terminal:

```bash
python swarm_corp.py "add a /health endpoint with a test"
```

Optionally point it at an existing repo for context:

```bash
python swarm_corp.py "add a /health endpoint" --repo C:\path\to\project
```

**What happens:** the Planner writes acceptance criteria, the Tester writes
tests against those criteria alone (never seeing the implementation), then
the Coder implements against both — its output is run through the sandbox,
audited once by Security, and reviewed by a Reviewer from a different model
family than the Coder. If Coder and Reviewer deadlock for 3 rounds, an
Arbiter breaks the tie. No role can force an APPROVE while the test suite is
red — that's enforced in code, not just prompted for.

Every run's transcript and generated code land in `swarm_output/<timestamp>/`
regardless of outcome.

If it doesn't get approved within `MAX_ROUNDS`, that's not a bug — it's the
signal to bring the transcript to Claude Code instead.

## Layout

- `swarm_corp.py` — the orchestration loop (all 6 roles, sandbox, gates)
- `providers.py` — multi-provider client (Groq, Cerebras, NVIDIA NIM, Gemini, Ollama)
- `sandbox.py` — runs Coder output through pytest/compileall for evidence-based verdicts
- `context.py` — repo file-tree/sample loader for `--repo`
- `verify_key.py` — run first, and any time something breaks
- `agents/*.md` — the 6 role personas (Planner, Tester, Coder, Security, Reviewer, Arbiter)
- `RUBRIC.md` — blocking vs. non-blocking defect guidance, loaded into the Reviewer's prompt
- `swarm_output/` — per-run transcripts and generated code (gitignored)
