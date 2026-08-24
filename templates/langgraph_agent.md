Domain: LangGraph/LangChain agent with human-in-the-loop approval gates.
Criteria hints: explicit state machine (nodes/edges, not an implicit loop),
at least one point where the agent must pause for human approval before a
side effect, and persistent state so a paused run can resume without
restarting. Test the approval gate specifically — a happy-path test that
never exercises the pause is not sufficient.
