# Security rules — deltas

Repo is public. Anything committed is public forever.

## Never commit secrets

- API keys, tokens, passwords → environment vars only. `${VAR}` expansion in config.
- `.env*` is `.gitignore`d. Don't override.
- Sample emails in examples are `you@example.com`. Real ones leak personal info.
- `local_files` `TraceStore` writes a `config.yaml` snapshot per run. **Mask any field whose name matches `*_KEY|*_TOKEN|*_SECRET|password|api_key`** before persisting. One implementation in the trace store, not per-adapter.

## Subprocess execution (cli adapter, command evaluator)

- `subprocess.run([list], shell=False, ...)`. Never `shell=True`. Never string-concat user input into a command.
- `cwd` pinned to the workspace artifact directory (NOT the source workspace — the system's already done by the time evaluators run).
- Bounded timeout. Capture stdout/stderr; don't stream unbuffered.

## Path handling

- `WorkspaceAdapter` owns `workspace.path`. Adapters/evaluators may not escape it.
- Before reading a path that came from a tool result or user input: `path.resolve().is_relative_to(workspace.path.resolve())`.

## HTTP

- HTTP adapters: validate scheme. `https://` for production, `http://localhost*` only for dev. Reject `file://`, `gopher://`, etc.
- Default timeout 60s. Never unbounded.
- Don't log full headers. `Authorization` and `x-api-key` are redacted at DEBUG.

## What the default `tempdir_snapshot` workspace is NOT

It snapshots filesystem state for diff purposes. **It does not sandbox a malicious agent.** For real isolation use the `docker_volume` workspace (lands in v1). Do not advertise `tempdir_snapshot` as sandboxing in docs or comments.

## Replay / online eval

Production traces may contain user PII. Configure platform-side redaction in the DatasetAdapter (`langfuse`, `phoenix`, etc.) — *not* after fetch (that leaves a window where PII is in our memory + on disk). Honor `Trace.extra.replayed_from` provenance; don't strip it.

## Forbidden

- `eval()` / `exec()` on anything from a config or tool result.
- `curl ... | sh`, `wget ... | sh`.
- `git commit --no-verify`, `--no-gpg-sign`, anything that bypasses hooks.
- `git push --force` to `main`.
- Modifying `.gitconfig` as part of a PR.

## Found a vulnerability mid-task?

Small + scoped → fix in the same PR.
Big or noisy → separate PR with the `security` label.
Either way: don't leave it.
