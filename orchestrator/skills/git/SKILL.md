---
name: git
description: Inspect a Git repository and prepare safe, reviewable local changes.
user-invocable: true
disable-model-invocation: false
---

Use Git only inside the current workspace.

- Inspect repository status before changing files.
- Do not rewrite history, force-push, or delete branches.
- Keep unrelated existing changes intact.
- Report changed files and verification performed.
- Do not create remote branches or commits unless the task explicitly requests it.

