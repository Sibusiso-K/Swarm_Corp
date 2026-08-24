"""
redact.py — strip secrets from any text before it can enter a prompt.

This is the highest-risk path Phase 4 opens: a read_file or web_fetch tool
result flows straight into a model prompt, and a prompt is sent to a
third-party API. One unredacted .env read ships every key in it to Groq/
Cerebras/whoever. Every tool result MUST go through redact() before it's
usable anywhere else.
"""

import re

# Order matters: more specific patterns first so a generic catch-all doesn't
# eat a match a specific pattern would have labeled more usefully.
_PATTERNS = [
    ("groq_key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("google_key", re.compile(r"AIza[A-Za-z0-9_\-]{30,}")),
    ("nvidia_key", re.compile(r"nvapi-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("generic_assignment", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)[A-Z0-9_]*)\s*[=:]\s*[\"']?([^\s\"'\n]{8,})[\"']?"
    )),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, list_of_pattern_names_that_matched).

    The caller should always check the second value — an empty list means
    nothing was found, not that redaction ran and found nothing suspicious
    to worry about (those are the same thing here, but callers auditing tool
    output should still log when a redaction actually fired)."""
    hit_names: list[str] = []

    def _sub(name: str, pattern: re.Pattern, s: str) -> str:
        def repl(m: re.Match) -> str:
            hit_names.append(name)
            if name == "generic_assignment":
                return f"{m.group(1)}=[REDACTED]"
            return "[REDACTED]"
        return pattern.sub(repl, s)

    out = text
    for name, pattern in _PATTERNS:
        out = _sub(name, pattern, out)
    return out, hit_names
