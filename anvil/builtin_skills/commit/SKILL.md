---
name: commit
description: Commit the current changes with a clear message, after checking what's staged and that nothing sensitive is included.
---
# Commit

1. Run `git status` and `git diff HEAD` to see everything that changed. Look at `git log -5 --oneline` to match the repository's message style.
2. Check for things that shouldn't be committed: secrets or keys (.env files, credentials, tokens in code), large build outputs, editor files. If you find any, stop and tell the user instead of committing them.
3. Stage the relevant files by name (avoid `git add -A` when unrelated files are modified).
4. Write the message: a short summary line in the imperative mood (under ~70 characters), a blank line, then a few lines on what changed and why if it isn't obvious.
5. Commit. Never use `--no-verify`, never amend or force-push unless the user asked. If a pre-commit hook fails, fix the issue and make a new commit.
6. Push only if the user asked, to the current branch.
