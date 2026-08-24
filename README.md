# Swarm_Corp

A 7-role coding swarm — Planner → Tester → Coder ⇄ Sandbox ⇄ Security/Architect/Reviewer
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
Every other provider is optional and widens the model pool. `verify_key.py`
does a real completion call, not just a `/models` check — a provider can
list a model it will then refuse to serve.

## Run

Double-click **Start Swarm_Corp** on the Desktop, or:

```bash
python swarm_corp.py "add a /health endpoint with a test"
```

### Flags

| Flag | Effect |
|---|---|
| `--repo PATH` | Give the Coder context from an existing repo; also enables cross-run memory |
| `--template NAME` | Preload the Planner with domain hints from `templates/NAME.md` |
| `--private` | Route every role to local Ollama; disable network tools. Nothing leaves the machine |
| `--plain` | Disable the Rich live UI (for CI/piping) |
| `--dry-run` | Never execute a gated side effect; log what would have run |
| `--allow CATS` | Pre-approve gate categories, comma-separated |
| `--allow-domains D` | Domains the Coder may `http_get`. **Empty = no fetches at all** (default deny) |

## What happens

The Planner writes acceptance criteria. The Tester writes tests against those
criteria *without seeing any implementation*. The Coder implements against
both — its output runs in a sandbox, gets audited once by Security and once
by the Architect (structural, informational only), then reviewed by a model
from a different family than the Coder. If Coder and Reviewer deadlock for
3 rounds, an Arbiter breaks the tie.

**No role can force an APPROVE while the test suite is red** — that's enforced
in code, not merely prompted for.

Each run writes to `swarm_output/`:
- `<timestamp>/` — the generated code, tests, and `ARCHITECTURE.md`
- `<timestamp>-<status>.md` — full round-by-round transcript
- `<timestamp>-<status>.json` — metrics (tokens per model, wall-clock, rounds)

If it isn't approved within `MAX_ROUNDS`, that's the signal to bring the
transcript to Claude Code, not to re-run it.

## Layout

- `swarm_corp.py` — orchestration loop, all 7 roles
- `providers.py` — multi-provider client (10 working providers + Ollama)
- `sandbox.py` — runs Coder output for evidence-based verdicts
- `tools.py` / `gates.py` / `redact.py` — tool access, human approval gates, secret redaction
- `ui.py` — Rich live streaming terminal
- `context.py` — `--repo` file-tree loader
- `agents/*.md` — the 7 role personas
- `templates/*.md` — domain-specific Planner hints
- `bench/` — prompt-strategy comparison harness
- `RUBRIC.md` — defect guidance, loaded into the Reviewer's prompt
- `verify_key.py` — run first, and any time something breaks

## Known limitations

- **`--private` is plumbing-complete but quality-limited.** `qwen2.5-coder:3b`
  doesn't reliably follow the `=== FILE: ===` output contract, so private runs
  often fail at the Tester step. Private mode is for work where the
  alternative is not using AI at all — not a quality upgrade.
- **`--private` breaks the family-diversity guarantee.** One local model means
  Coder and Reviewer share a family. The run says so out loud rather than
  letting it pass silently.
- **Tool calls are bounded**: the Coder gets at most 2 info-gathering rounds,
  and only free/unattended tools are dispatched automatically. Gated tools
  (`git push`, `shell`, writes outside the workspace) exist in `tools.py` but
  are never auto-invoked mid-run.
- **Not implemented**: hooks (pre/post interception), container-isolated
  sandboxing, MCP tool interfaces. Each needs its own design pass.
