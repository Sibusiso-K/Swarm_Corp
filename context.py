"""
context.py — load repo file tree and sample existing code.
Prepend to Coder prompt so it has context instead of asking "what's in the repo?"

Budget: ~1500 tokens max (samples + tree).
"""

from pathlib import Path


def load_repo_context(repo_path: str, token_budget: int = 1500) -> str:
    """Load repo tree and file samples. Returns markdown context block.

    Budget estimate: ~3.3 chars per token, so token_budget * 3 chars available.
    """
    repo = Path(repo_path).resolve()
    if not repo.exists():
        return f"(repo not found at {repo_path})"

    available_chars = token_budget * 3

    # Build file tree
    tree_lines = [f"Repository: {repo.name}/"]
    all_files = sorted(repo.glob("**/*"))
    py_files = [f for f in all_files if f.is_file() and f.suffix in {".py", ".js", ".ts", ".md"}][:20]

    for f in py_files:
        rel = f.relative_to(repo)
        try:
            lines = f.read_text().count("\n")
            tree_lines.append(f"  {rel} ({lines} lines)")
        except Exception:
            tree_lines.append(f"  {rel}")

    tree_section = "\n".join(tree_lines)
    chars_used = len(tree_section)

    if chars_used >= available_chars:
        return f"File tree (truncated):\n{tree_section[:available_chars]}"

    # Add samples from key files
    samples = []
    for name in ["README.md", "setup.py", "pyproject.toml", "package.json", "conftest.py"]:
        if chars_used >= available_chars:
            break
        path = repo / name
        if path.is_file():
            try:
                content = path.read_text()
                sample = content[:min(400, (available_chars - chars_used) // 2)]
                samples.append(f"\n{name}:\n{sample}...")
                chars_used += len(sample)
            except Exception:
                pass

    samples_section = "\n".join(samples) if samples else ""
    return f"File tree:\n{tree_section}{samples_section}"
