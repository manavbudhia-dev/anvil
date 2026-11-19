---
name: review
description: Review the current changes (or a given path) for bugs, security problems and risky patterns, ranked by severity with file:line references.
---
# Code review

1. Find what to review: if the user named files or a commit range, use that. Otherwise use the uncommitted changes (`git diff HEAD`, plus untracked files from `git status --porcelain`); if there are none, the last commit (`git show HEAD`).
2. Read enough surrounding code to understand each change: callers, the types involved, tests that cover it.
3. Look for, in priority order: correctness bugs (wrong logic, off-by-one, unhandled None/null, race conditions, broken error handling), security issues (injection, path traversal, secrets in code, missing auth checks, unsafe deserialization), data loss risks, performance problems on hot paths, then missing tests for risky logic.
4. Verify before reporting: for each suspected bug, trace a concrete input that triggers it. Drop anything you can't substantiate. Run the tests or a quick script when that settles a question.
5. Report findings most severe first. For each: `path:line`, one sentence on the problem, the concrete failure scenario, and a suggested fix. Skip style nitpicks unless asked. If you found nothing significant, say so plainly.
6. Don't change code unless the user asks you to fix the findings.
