"""
tools.py — file/web/git/http access for the swarm, split into free reads
and gated side effects. Every tool result that could contain a secret goes
through redact.py before returning. Every path-taking tool is confined to
an allowlisted root — no ../ escapes, no symlink escapes.

NOT YET WIRED into the Planner/Coder loop: swarm_corp.py's orchestration is
a fixed pipeline (Planner -> Tester -> Coder -> Sandbox -> Review), not an
agentic tool-calling loop. These functions are built, unit-tested, and ready
to call, but nothing in run_swarm() invokes them yet — that integration
(giving the Coder a way to actually request a tool call mid-generation, then
feeding the result back) is follow-up work, not done here.
"""

from __future__ import annotations

import functools
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Callable

from redact import redact
from gates import approve

ALLOWED_ROOTS: list[Path] = []  # populated by configure()
ALLOWED_DOMAINS: set[str] = set()


def configure(allowed_roots: list[Path], allowed_domains: set[str] | None = None) -> None:
    global ALLOWED_ROOTS, ALLOWED_DOMAINS
    ALLOWED_ROOTS = [r.resolve() for r in allowed_roots]
    ALLOWED_DOMAINS = allowed_domains or set()


def _confine(path_str: str) -> Path:
    """Resolve a path and verify it's inside an allowed root. Raises
    PermissionError otherwise. Resolving symlinks (Path.resolve()) before
    the prefix check is what stops a symlink from pointing outside the
    allowlist and slipping past a naive string check."""
    p = Path(path_str).resolve()
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise PermissionError(f"Path '{path_str}' resolves outside all allowed roots: {ALLOWED_ROOTS}")


def _deny_outside_root(fn: Callable) -> Callable:
    """_confine() raises PermissionError on an out-of-root path. Every tool
    function below needs that to become a clean {"ok": False, "error": ...}
    dict, the same shape every other failure returns — an uncaught exception
    here would crash the caller instead of just denying the one call."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc)}
    return wrapper


def _is_inside_any_root(path_str: str) -> bool:
    try:
        _confine(path_str)
        return True
    except PermissionError:
        return False


# ---------------------------------------------------------------------------
# Free (read-only, or writes confined to the first allowed root — the
# workspace)
# ---------------------------------------------------------------------------

@_deny_outside_root
def read_file(path_str: str, max_chars: int = 20000) -> dict[str, Any]:
    p = _confine(path_str)
    if not p.is_file():
        return {"ok": False, "error": f"not a file: {path_str}"}
    text = p.read_text(encoding="utf-8", errors="replace")[:max_chars]
    redacted, hits = redact(text)
    return {"ok": True, "content": redacted, "redacted_patterns": hits}


@_deny_outside_root
def list_dir(path_str: str) -> dict[str, Any]:
    p = _confine(path_str)
    if not p.is_dir():
        return {"ok": False, "error": f"not a directory: {path_str}"}
    return {"ok": True, "entries": sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())}


@_deny_outside_root
def grep(path_str: str, pattern: str, max_matches: int = 50) -> dict[str, Any]:
    import re
    p = _confine(path_str)
    rx = re.compile(pattern)
    matches: list[str] = []
    files = [p] if p.is_file() else list(p.rglob("*.py")) + list(p.rglob("*.md"))
    for f in files:
        if len(matches) >= max_matches:
            break
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{f}:{i}: {line.strip()[:200]}")
                    if len(matches) >= max_matches:
                        break
        except Exception:
            continue
    redacted_matches = [redact(m)[0] for m in matches]
    return {"ok": True, "matches": redacted_matches}


def write_file(path_str: str, content: str) -> dict[str, Any]:
    """Free only inside the workspace (ALLOWED_ROOTS[0] by convention).
    Anything outside that must go through write_file_gated."""
    workspace = ALLOWED_ROOTS[0] if ALLOWED_ROOTS else None
    if workspace is None:
        return {"ok": False, "error": "no workspace root configured"}
    p = Path(path_str).resolve()
    try:
        p.relative_to(workspace)
    except ValueError:
        return {"ok": False, "error": "outside workspace — use write_file_gated"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True}


def run_tests(workspace_root: Path, timeout_sec: int = 30) -> dict[str, Any]:
    from sandbox import sandbox_run
    return sandbox_run(workspace_root, timeout_sec=timeout_sec)


def git_status(repo_path: str) -> dict[str, Any]:
    return _git_readonly(repo_path, ["status", "--short"])


def git_diff(repo_path: str) -> dict[str, Any]:
    return _git_readonly(repo_path, ["diff"])


def git_log(repo_path: str, n: int = 10) -> dict[str, Any]:
    return _git_readonly(repo_path, ["log", f"-{n}", "--oneline"])


@_deny_outside_root
def _git_readonly(repo_path: str, args: list[str]) -> dict[str, Any]:
    p = _confine(repo_path)
    try:
        result = subprocess.run(["git", "-C", str(p)] + args, capture_output=True, text=True, timeout=15)
        out, _ = redact(result.stdout)
        return {"ok": result.returncode == 0, "output": out, "stderr": result.stderr}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def http_get(url: str, timeout_sec: int = 10) -> dict[str, Any]:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if ALLOWED_DOMAINS and not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
        return {"ok": False, "error": f"domain '{host}' not in allowlist {ALLOWED_DOMAINS}"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Swarm_Corp/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read(200_000).decode("utf-8", errors="replace")
        redacted, hits = redact(body)
        # Fetched content is DATA, never instructions — wrap it so a page
        # containing "ignore previous instructions" can't be mistaken for
        # part of the system/task prompt by whatever consumes this result.
        wrapped = f"<fetched_content url=\"{url}\">\n{redacted}\n</fetched_content>"
        return {"ok": True, "content": wrapped, "redacted_patterns": hits}
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Gated (side effects — every one of these must go through approve() first)
# ---------------------------------------------------------------------------

def write_file_gated(path_str: str, content: str) -> dict[str, Any]:
    if not _is_inside_any_root(path_str):
        return {"ok": False, "error": f"'{path_str}' is outside every allowed root, even for a gated write"}
    if not approve("write_outside_workspace", f"Write {len(content)} chars to {path_str}"):
        return {"ok": False, "error": "not approved"}
    p = Path(path_str).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True}


@_deny_outside_root
def git_commit(repo_path: str, message: str) -> dict[str, Any]:
    p = _confine(repo_path)
    if not approve("git_commit", f"git commit -m {message!r} in {p}"):
        return {"ok": False, "error": "not approved"}
    result = subprocess.run(["git", "-C", str(p), "commit", "-am", message], capture_output=True, text=True, timeout=15)
    return {"ok": result.returncode == 0, "output": result.stdout, "stderr": result.stderr}


@_deny_outside_root
def git_push(repo_path: str) -> dict[str, Any]:
    p = _confine(repo_path)
    if not approve("git_push", f"git push from {p}"):
        return {"ok": False, "error": "not approved"}
    result = subprocess.run(["git", "-C", str(p), "push"], capture_output=True, text=True, timeout=60)
    return {"ok": result.returncode == 0, "output": result.stdout, "stderr": result.stderr}


def http_write(url: str, method: str, body: str = "") -> dict[str, Any]:
    if method.upper() not in {"POST", "PUT", "DELETE", "PATCH"}:
        return {"ok": False, "error": f"unsupported method: {method}"}
    if not approve("network_write", f"{method.upper()} {url} (body: {len(body)} chars)"):
        return {"ok": False, "error": "not approved"}
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method=method.upper())
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"ok": True, "status": resp.status, "body": resp.read(50_000).decode("utf-8", errors="replace")}
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        return {"ok": False, "error": str(exc)}


def shell(cmd: list[str], cwd: str | None = None, timeout_sec: int = 30) -> dict[str, Any]:
    """Arbitrary shell/install commands. Always gated, no exceptions —
    this is the one tool with no useful "safe subset" to leave ungated."""
    if not approve("shell", f"Run: {' '.join(cmd)}" + (f" (cwd={cwd})" if cwd else "")):
        return {"ok": False, "error": "not approved"}
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_sec)
        return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
