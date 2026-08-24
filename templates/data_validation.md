Domain: Data validation / silent-error prevention.
Criteria hints: the goal is catching the class of bug that produces a
plausible-looking WRONG answer rather than an obvious crash — validate
assumptions explicitly (schema, ranges, expected cardinality) and fail
loudly on violation rather than silently continuing with bad data.
Include a test that feeds deliberately malformed input and asserts it's
rejected, not just tests that feed valid input.
