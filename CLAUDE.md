# Swarm_Corp — project brief

A 6-role coding swarm running entirely on free LLM provider tiers, with
Claude Code as the manual escalation lane when the swarm can't close a task
on its own. Two things exist in this project. Nothing else:

1. A terminal running `swarm_corp.py` (or the "Start Swarm_Corp" Desktop
   launcher, which is a thin `.bat` wrapper around the same command).
2. Claude Code, open beside it, for anything the swarm punts on.

That's it. There is no Kilo Code, no jcode, no other harness or IDE
extension in this project's design. If you find a doc or comment implying
otherwise, it's stale — fix it to match this file.

## The 6 roles

Planner → Tester → **[** Coder ⇄ Sandbox ⇄ Security/Reviewer **]** → Arbiter

- **Planner** writes acceptance criteria from the task.
- **Tester** writes tests against those criteria *before* seeing any
  implementation — this is what makes the tests an independent check
  instead of the Coder grading its own homework.
- **Coder** implements against criteria + tests + prior-round feedback, up
  to `MAX_ROUNDS` (4) attempts.
- **Sandbox** (`sandbox.py`) actually runs the tests via `sys.executable`
  (never a bare `"python"` string — that resolves to system Python, not the
  venv, and silently never runs anything).
- **Security** audits once, after the first round that passes tests.
- **Reviewer** — a different model *family* than the Coder, enforced at
  startup — audits the implementation.
- **Arbiter** breaks a round-3 deadlock, and sees the same test evidence
  the Reviewer did.

**No role can force an APPROVE while the test suite is red.** This is a
hard gate in code (`run_swarm()`), applied after every verdict extraction —
Reviewer, the UNPARSEABLE retry path, and the Arbiter alike. Personas are
*told* to reject on red tests, but a prompt instruction is advisory; only
code enforcement actually stopped a false approve from shipping once.

## Hard constraints

- **Free tiers only.** No paid API keys required for the default path.
  `GROQ_API_KEY` in `.env` is the only credential the swarm strictly needs;
  `CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, and `GEMINI_API_KEY` are optional
  and widen the model pool. See `.env.example`.
- **No paid API keys, period, for the swarm itself.** The dormant
  Anthropic-provider code in `swarm_corp.py` (`get_anthropic_client`,
  gated behind `SWARM_TIER`) stays inert. `SWARM_TIER=free` is the default
  and nothing in the normal run path calls it. This project does not spend
  on the Anthropic API — Claude Code (an existing Pro subscription) is the
  escalation lane instead, not another metered provider.
- **Per-provider TPM caps, enforced per model.** Every completion call is
  capped against its own provider's real free-tier limit — Groq's ~8000
  TPM per model, Cerebras's 60000, and so on (`providers.PROVIDERS`,
  `TokenBudget` in `swarm_corp.py`). A cap borrowed from the wrong provider
  either starves a generous budget or blows through a tight one.
- **Coder != Tester != Reviewer != Arbiter, by model family.** Checked at
  startup in `resolve_models()`, not assumed. A model reviewing (or
  testing) its own family's output tends to miss exactly what that family
  is bad at. The same open-weight model can appear under different slug
  shapes on different providers (e.g. Groq's `openai/gpt-oss-120b` vs.
  Cerebras's bare `gpt-oss-120b`) — `family_of()` normalises known
  multi-provider names first, specifically so two copies of the same model
  can't satisfy this check by accident.

## A live model listing is not proof it serves

Live testing against real keys found providers whose `/models` catalog
listed models that then failed at inference: Cerebras returned 402
(payment_required — free trial not active on that account) on every model
it listed; NVIDIA NIM listed 100+ models but only one actually answered on
that account, the rest 404/410'd as "not found for account." `verify_key.py`
does a real completion call against the resolved Coder model for exactly
this reason — `/models` returning 200 means "this exists," not "this
works for you."

`MODEL_REGISTRY` in `swarm_corp.py` orders candidates with this in mind:
providers proven to answer come first per role, unverified/unreliable ones
are pushed to the end so they only get tried as a last resort (and start
winning automatically if you later fix billing/access on that account).

## Escalation lane

If the swarm can't get a task approved after `MAX_ROUNDS`, it stops, writes
its last attempt and the Reviewer's/Arbiter's objections to `swarm_output/`,
and exits non-zero. That's the signal to open the same task in Claude Code
rather than spending more free-tier tokens re-running a loop that's already
demonstrated it's stuck.

## Layout

```
Swarm_Corp/
├── swarm_corp.py         # the whole orchestration loop, all 6 roles
├── providers.py          # multi-provider client abstraction
├── sandbox.py            # runs Coder output for evidence-based verdicts
├── context.py            # --repo file-tree/sample loader
├── verify_key.py         # run this first, and any time something breaks
├── agents/
│   ├── planner.md
│   ├── tester.md
│   ├── coder.md
│   ├── security.md
│   ├── reviewer.md
│   └── arbiter.md
├── RUBRIC.md              # loaded into the Reviewer's prompt at runtime
├── requirements.txt
├── .env.example           # copy to .env, paste provider keys in
├── Start Swarm_Corp.bat   # Desktop-launcher entry point
└── swarm_output/          # every run's transcript + code (gitignored)
```

## Architecture note

`swarm_corp.py` does not use AutoGen. AutoGen's agent-chat API
(`TerminationCondition`, `StopMessage`, and friends) has moved across
package versions enough times that depending on it was making this project
more fragile than the problem it's solving. The orchestration loop here is
a direct, minimal multi-role cycle against a single shared `complete()`
function that dispatches to whichever provider a role resolved to via
`providers.get_client()`. If a future version of this project adopts
AutoGen anyway, update this section and re-verify the import paths against
whatever AutoGen version is current at that time — don't assume last
year's paths still work.
