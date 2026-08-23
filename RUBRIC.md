# Defect Rubric

When the Reviewer encounters issues in the Coder's submission, classify them as **blocking** or **non-blocking**. Blocking defects prevent approval; non-blocking defects are feedback for improvement but don't block.

## Blocking Defects

- **Compilation/Syntax Error** — Code doesn't parse or compile
- **Test Failure** — Tests run but fail (logic error, not infrastructure)
- **Security Issue** — Passwords in code, SQL injection, XSS, unsafe deserialization, etc.
- **Task Incompleteness** — Code doesn't fulfill the stated task at all
- **Unsafe File Operations** — Attempts to write outside the workspace (path traversal)

## Non-Blocking Defects

- **Missing Edge Cases** — Logic is sound but incomplete for edge cases
- **Inefficient Algorithm** — Works but could be faster (only block if perf is the task)
- **Code Style** — Formatting, naming, minor structure issues
- **Missing Comments** — Code is clear but lacks documentation
- **Scope Creep** — Added features beyond the task (if not harmful)

## Approval Criteria

- **APPROVE if:** No blocking defects + tests pass (if applicable)
- **REJECT if:** Any blocking defect found

## Reviewer Guidance

If a submission has only non-blocking defects, you may still REJECT and ask for fixes if you judge the Coder capable of addressing them quickly (e.g., "missing test coverage for the main case"). But prefer APPROVE + feedback when the core task is done correctly, to avoid round burnout on polish.
