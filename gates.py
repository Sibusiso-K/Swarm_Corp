"""
gates.py — the human-only approval checkpoint for anything with a side
effect (write outside the workspace, network write, git push, shell/install).

No model output can satisfy a gate. approve() always blocks on real
keyboard input unless --dry-run or --allow pre-cleared the category, both
of which are process-level flags the human set before the run started, not
anything a model can influence mid-run.
"""


_dry_run = False
_pre_allowed: set[str] = set()
_audit_log: list[dict] = []


def configure(dry_run: bool = False, pre_allow: set[str] | None = None) -> None:
    global _dry_run, _pre_allowed
    _dry_run = dry_run
    _pre_allowed = pre_allow or set()


def approve(category: str, description: str) -> bool:
    """Ask for human approval of a gated action. Returns True only if the
    human (or a pre-run --allow flag, or --dry-run) actually cleared it.

    category: short tag like "write_outside_workspace", "git_push",
    "network_write", "shell". description: the specific thing about to
    happen, shown verbatim to the human — never summarized or paraphrased,
    since a summary is a place for a manipulated model output to hide."""
    decision: bool
    reason: str

    if _dry_run:
        decision, reason = False, "dry-run: not executed"
    elif category in _pre_allowed:
        decision, reason = True, "pre-approved via --allow"
    else:
        print(f"\n[GATE: {category}]")
        print(f"  {description}")
        reply = input("  Approve? [y/N] ").strip().lower()
        decision = reply == "y"
        reason = "human approved" if decision else "human declined"

    _audit_log.append({"category": category, "description": description, "approved": decision, "reason": reason})
    return decision


def audit_log() -> list[dict]:
    return list(_audit_log)


def write_audit_log(path) -> None:
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_audit_log, f, indent=2)
