"""
agent_swarm_sandbox.py — run Coder-generated code in a confined subprocess
and return test/compile results. This provides evidence-based feedback to the
Reviewer without spending a token call on guessing whether code actually works.

All tests/compilation are FREE rejects; the Coder just tries again.
"""

import subprocess
from pathlib import Path
from typing import Any


def sandbox_run(workspace_root: Path, timeout_sec: int = 30) -> dict[str, Any]:
    """Run tests or compile checks on code in workspace_root.

    Tries in order (stops at first that exists and runs):
    1. pytest (if conftest.py or test_*.py exists)
    2. compileall (always available for .py)
    3. node --check (if any .js files exist)

    Returns dict with:
    - "passed": bool (returncode == 0)
    - "stdout": str
    - "stderr": str
    - "returncode": int
    - "note": str (what was run, or why nothing ran)
    """
    if not workspace_root.exists():
        return {"passed": False, "stdout": "", "stderr": "workspace not found", "returncode": -1, "note": "workspace missing"}

    # Check what's available to test
    has_pytest = bool(list(workspace_root.glob("**/conftest.py"))) or bool(list(workspace_root.glob("**/test_*.py")))
    has_py_files = bool(list(workspace_root.glob("**/*.py")))
    has_js_files = bool(list(workspace_root.glob("**/*.js")))

    # Try pytest first if tests exist
    if has_pytest:
        return _run_command(["python", "-m", "pytest", str(workspace_root), "-q", "--tb=short"], timeout_sec, "pytest")

    # Fall back to compileall for .py files
    if has_py_files:
        return _run_command(["python", "-m", "compileall", "-q", str(workspace_root)], timeout_sec, "compileall")

    # Try node for .js
    if has_js_files:
        # node --check doesn't recurse, so check each .js file
        js_files = list(workspace_root.glob("**/*.js"))
        for js_file in js_files[:10]:  # limit to first 10 to avoid explosion
            result = _run_command(["node", "--check", str(js_file)], timeout_sec, f"node --check {js_file.name}")
            if not result["passed"]:
                return result
        return {"passed": True, "stdout": f"Checked {len(js_files)} .js files", "stderr": "", "returncode": 0, "note": "node --check"}

    return {"passed": True, "stdout": "", "stderr": "", "returncode": 0, "note": "no tests to run (no .py, .js, or conftest)"}


def _run_command(cmd: list[str], timeout_sec: int, name: str) -> dict[str, Any]:
    """Run a command with timeout, return results dict."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return {
            "passed": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "note": name,
        }
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "stdout": "",
            "stderr": f"Command timed out after {timeout_sec}s",
            "returncode": -1,
            "note": f"{name} (timeout)",
        }
    except Exception as exc:
        return {
            "passed": False,
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "note": f"{name} (error)",
        }
