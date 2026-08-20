---
name: python
description: Implement and verify Python code in the current workspace.
user-invocable: true
disable-model-invocation: false
---

When working with Python:

- Follow the repository's existing style and dependency choices.
- Prefer focused edits and deterministic tests.
- Run the narrowest relevant checks, then broader tests when practical.
- Do not install packages globally or modify paths outside the workspace.
- Report commands run and any checks that could not be completed.

