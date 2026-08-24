"""
ui.py — live streaming terminal for Swarm_Corp, isolated from swarm_corp.py
so the orchestration loop doesn't know or care whether Rich is installed or
a TTY is attached.

Two renderers behind one interface (RENDERER):
- RichUI: token-by-token streaming panel per role, with <think> blocks shown
  dimmed instead of stripped-and-discarded (they're already generated; the
  old code threw them away before this).
- PlainUI: the original print()-per-line behavior, used for --plain, CI, or
  whenever Rich/a real terminal isn't available. Output stays pipeable.

swarm_corp.py only ever calls the module-level functions below (start_role,
stream_chunk, end_role, status, note) — never touches Rich directly.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

_THINK_RE = re.compile(r"(<think>.*?</think>)", re.DOTALL)

_use_rich = False
_live = None
_console = None
_current_role = ""
_current_model = ""
_current_text = ""


def _try_import_rich():
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.panel import Panel
        from rich.text import Text
        return Console, Live, Panel, Text
    except ImportError:
        return None


def init(plain: bool = False) -> None:
    """Call once at startup. Falls back to plain mode if Rich isn't
    installed or stdout isn't a real terminal (streaming panels make no
    sense piped into a file or CI log)."""
    global _use_rich, _console
    if plain or not sys.stdout.isatty():
        _use_rich = False
        return
    imported = _try_import_rich()
    if imported is None:
        _use_rich = False
        return
    Console, _, _, _ = imported
    _console = Console()
    _use_rich = True


def _render_thinking(text: str):
    """Split accumulated text on <think>...</think> and render think blocks
    dimmed. Called on every redraw rather than styled incrementally per
    token — the buffer is at most a few thousand chars, so re-splitting each
    frame is cheap and far simpler than tracking open/close state across
    chunk boundaries."""
    from rich.text import Text
    out = Text()
    parts = _THINK_RE.split(text)
    for part in parts:
        if part.startswith("<think>") and part.endswith("</think>"):
            inner = part[len("<think>"):-len("</think>")]
            out.append(inner, style="dim italic")
        else:
            out.append(part)
    return out


def start_role(role: str, model_ref: str) -> None:
    global _current_role, _current_model, _current_text
    _current_role = role
    _current_model = model_ref
    _current_text = ""

    if not _use_rich:
        print(f"  {role} is working... ({model_ref})")
        return

    from rich.live import Live
    from rich.panel import Panel
    global _live
    panel = Panel("", title=f"[bold]{role}[/bold] — {model_ref}", border_style="cyan")
    _live = Live(panel, console=_console, refresh_per_second=8)
    _live.start()


def stream_chunk(delta: str) -> None:
    global _current_text
    _current_text += delta
    if not _use_rich or _live is None:
        return
    from rich.panel import Panel
    body = _render_thinking(_current_text)
    _live.update(Panel(body, title=f"[bold]{_current_role}[/bold] — {_current_model}", border_style="cyan"))


def end_role(verdict: Optional[str] = None) -> None:
    global _live
    if not _use_rich:
        if verdict:
            print(f"  Verdict: {verdict}")
        return
    if _live is not None:
        _live.stop()
        _live = None
    if verdict and _console is not None:
        color = "green" if verdict == "APPROVE" else "red" if verdict == "REJECT" else "yellow"
        _console.print(f"  Verdict: [{color}]{verdict}[/{color}]")


def status(message: str) -> None:
    """One-line status update, e.g. round counters, TPM waits, auto-rejects.
    Not part of a streaming panel — just needs to show up in order."""
    if _use_rich and _console is not None:
        _console.print(f"[dim]{message}[/dim]")
    else:
        print(message)


def note(message: str) -> None:
    """Plain informational line — provider list, model assignments, etc."""
    if _use_rich and _console is not None:
        _console.print(message)
    else:
        print(message)
