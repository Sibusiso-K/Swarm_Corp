# swarm-lab — project brief

An autonomous Coder/Reviewer swarm running on Groq's free tier, with Claude
Code as the manual escalation lane when the swarm can't close a task on its
own. Two things exist in this project. Nothing else:

1. A terminal running `agent_swarm.py`.
2. Claude Code, open beside it, for anything the swarm punts on.

That's it. There is no Kilo Code, no jcode, no other harness or IDE
extension in this project's design. If you find a doc or comment implying
otherwise, it's stale — fix it to match this file.

## Hard constraints

- **Free Groq only.** No paid API keys required for the default path.
  `GROQ_API_KEY` in `.env` is the only credential the swarm needs to run.
- **No paid API keys, period, for the swarm itself.** The dormant
  Anthropic-provider code in `agent_swarm.py` (`get_anthropic_client`,
  gated behind `SWARM_TIER`) stays inert. `SWARM_TIER=free` is the default
  in `.env.example` and nothing in the normal run path calls it. This
  project does not spend on the Anthropic API — Claude Code (an existing
  Pro subscription) is the escalation lane instead, not another metered
  provider.
- **8K TPM per request.** Every single completion call is capped at 8000
  tokens, prompt + completion combined. This isn't a suggestion — it's
  enforced in code (`TokenBudget` in `agent_swarm.py`), because free-tier
  Groq rate limits are real and a swarm that ignores them just 429s
  itself into uselessness.
- **Reviewer != Coder's model family.** Checked at startup, not assumed.
  A model reviewing its own family's output tends to miss exactly the
  things that family is bad at — the entire value of a second model is
  that it doesn't share the first one's blind spots. `resolve_models()`
  raises if this constraint isn't met; the swarm refuses to run rather
  than silently degrade into a rubber stamp.

## Model slugs drift

Groq's available models change more often than this file does. Don't trust
a hardcoded slug list to still be correct — `verify_key.py` checks
`MODEL_REGISTRY` (in `agent_swarm.py`) against Groq's live `/models`
endpoint every time you run it, and picks the first candidate per role
that's actually live. If a role comes back with zero live candidates, add
a new candidate to that role's list in `MODEL_REGISTRY` and re-run
`verify_key.py`. Check https://console.groq.com/docs/models for current
names if you're not sure what's available.

## Escalation lane

If the swarm can't get a task approved after `MAX_ROUNDS` (4, by default —
see `agent_swarm.py`), it stops, writes its last attempt and the Reviewer's
objections to `swarm_output/`, and exits non-zero. That's the signal to
open the same task in Claude Code rather than spending more free-tier
tokens re-running a loop that's already demonstrated it's stuck.

## Layout

```
swarm-lab/
├── agent_swarm.py       # the whole orchestration loop
├── verify_key.py        # run this first, and any time something breaks
├── agents/
│   ├── orchestrator.md  # persona, not currently wired into the loop
│   ├── coder.md
│   └── reviewer.md
├── requirements.txt
├── .env.example          # copy to .env, paste GROQ_API_KEY in
└── swarm_output/          # every run's transcript lands here (gitignored)
```

## Architecture note

`agent_swarm.py` does not use AutoGen. AutoGen's agent-chat API
(`TerminationCondition`, `StopMessage`, and friends) has moved across
package versions enough times that depending on it was making this project
more fragile than the problem it's solving. The orchestration loop here is
a direct, minimal Coder → Reviewer → revise cycle against the `groq` SDK's
stable `chat.completions.create` interface. If a future version of this
project adopts AutoGen anyway, update this section and re-verify the
import paths against whatever AutoGen version is current at that time —
don't assume last year's paths still work.
