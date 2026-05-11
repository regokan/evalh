# Git rules — deltas

## Conventional Commits

Commit subject: `<type>(<scope>): <imperative summary>`, ≤72 chars.

Body (optional): wrap at 72; explain *why*, not *what*.

Types: `feat | fix | refactor | test | docs | chore | build | ci`.
Scopes are package directories: `runner`, `core`, `cli`, `http_adapter`, `python_function_adapter`, `tempdir_workspace`, `local_files_store`, `llm_judge`, `tool_called`, `contains_text`, `factories`, `reports`, `examples`, `tests`, `docs`.

## When a pre-commit hook fails

The commit didn't happen. **Don't `--amend`** — that touches the previous (good) commit. Fix the issue, `git add`, re-run `git commit`. Fresh commit on top.

## Forbidden without explicit human authorization

- `git push --force` to `main` (or any shared branch you didn't create).
- `git commit --no-verify`, `--no-gpg-sign`, `-c commit.gpgsign=false`.
- `git commit --amend` after pushing.
- `git config` writes (the user's config is theirs).
- Deleting tags, releases, or branches you didn't create.
