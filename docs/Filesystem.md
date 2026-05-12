# Filesystem Evaluation

For most evals, the trace is enough — final answer, tool calls, tool results. Pure-conversation agents and RAG systems never write to disk.

But coding agents do. So do report-generating agents, file-uploading workflows, and anything that runs `mkdir`. For those, the eval needs to inspect the filesystem.

The rule:

> **Trace first. Filesystem access only through an explicit `WorkspaceAdapter`.**

The runner does not have raw access to your filesystem. The system under test does its work inside a workspace. The workspace adapter is the only thing that knows where files are. Evaluators see a `FilesystemArtifact`, not a `Path`.

---

## Two cases

### Case A: agent answers questions / calls tools

No filesystem needed. The trace is the truth.

```yaml
trace:
  capture: [final_answer, tool_calls, tool_results, messages, latency, tokens]
```

No `workspace:` key. No `WorkspaceAdapter` instantiated. No diff captured.

### Case B: agent modifies files (coding agents, report writers, etc.)

```yaml
workspace:
  type: tempdir_snapshot
  base_path: /tmp/eval-harness-workspaces
  copy_from: ./fixture_repo

evaluators:
  - name: changed_the_right_files
    type: git_diff                   # or `command` for "ran tests successfully"
    config:
      expected_modified: ["src/pricing.py"]
      forbidden_modified: ["README.md"]
```

Now the runner builds a workspace, hands its path to the system adapter, and after the run captures a `FilesystemArtifact` for the evaluators to read.

---

## The no-git path: `tempdir_snapshot`

The user's question:

> *How can we find filesystem diff? Can we assume mac and do that? There is no git. And there should be extension to add git support to it.*

Yes. Here's how.

### Algorithm

```mermaid
flowchart TB
    PREP[prepare(case, variant)] -- "1. mktemp -d /tmp/eh-<case>-<variant>" --> WS[Workspace path]
    WS -- "2. recursively copy copy_from/ into workspace" --> SEED[Seeded workspace]
    SEED -- "3. walk workspace, build FileManifest<br/>for every file: size, mode, mtime, sha256" --> BEFORE[before_manifest]
    BEFORE -- "4. workspace.path handed to SystemAdapter" --> RUN[System runs, may modify files]
    RUN -- "5. collect_artifacts(): walk again" --> AFTER[after_manifest]
    AFTER -- "6. diff manifests by path + sha256" --> DIFF[FileDiff: added / removed / modified]
    DIFF -- "7. for text files in 'modified': compute unified diff via difflib" --> TEXT[text_diffs]
    TEXT -- "8. assemble FilesystemArtifact" --> ART[Artifact handed to evaluators]
    ART -- "9. cleanup(): rm -rf workspace<br/>but copy artifacts/ into run_dir first" --> CLEAN[Workspace gone]
```

### Why it works without git

- Snapshot before, snapshot after. The diff is `set difference` on `(path, sha256)` pairs.
- `sha256` (or BLAKE3 if available; faster on macOS) gives content-addressable identity.
- File mode + size + mtime are stored too — useful for evaluators that care about permissions or timestamps.
- For text files, we additionally produce a unified diff body (via Python's `difflib.unified_diff`). Binary files are reported as "modified" without a body.

### Manifest shape

```python
class FileManifest(BaseModel):
    files: dict[str, FileEntry]   # path relative to workspace root

class FileEntry(BaseModel):
    size: int
    mode: int
    mtime: float
    sha256: str

class FileDiff(BaseModel):
    added: list[str]
    removed: list[str]
    modified: list[str]
    text_diffs: dict[str, str] = {}
```

### macOS-specific accelerators (later)

- **`fseventsd`** can give us live "what changed" events without re-walking. The first version walks twice; we can swap in `fsevents` later for big workspaces.
- **APFS clone copies** make seeding cheap: `cp -c` on macOS uses copy-on-write so the initial seed of a 100 MB fixture is microseconds. Use `cp -c` when the fixture is on the same APFS volume.
- **`mdfind`** is too coarse for diffs but useful for sanity checks.

None of these are required for v0. The walk-and-hash approach works portably on macOS, Linux, and CI.

### Why not git by default

- Git turns "snapshot a directory" into "init a repo, add files, commit, status, diff." Slower, more state, more failure modes.
- Many fixtures aren't git repos and shouldn't be (they're fixtures).
- A user's home directory is often a git repo; reusing it would conflate eval state with their work.
- `tempdir_snapshot` works on systems without git installed (CI minimal images).

So the default does not require git, and it does not pollute anything.

---

## The git path: `WorkspaceAdapter` of type `git`

When a user wants richer diffs, branch-aware artifacts, or commit metadata, they opt in:

```yaml
workspace:
  type: git
  base_path: /tmp/eval-harness-workspaces
  source_repo: ./my-agent-fixtures
  source_branch: main
  init_if_missing: true        # if source isn't a git repo, init one in the tempdir
```

What changes:
- Seed = `git clone --depth 1 --branch <branch>` instead of `cp -c`.
- After-snapshot = `git add -A && git diff --staged` instead of walking + hashing.
- Artifact includes `git_before` (commit), `git_after` (a synthetic commit on a temp branch), and a real `.patch`.

Same `FilesystemArtifact` schema. Evaluators don't have to know which workspace adapter produced it.

This is the extension model:
- v0 default: `tempdir_snapshot` (no git).
- v0.1 add: `git` workspace adapter (opt-in).
- v1 add: `docker_volume` workspace adapter (full sandbox).

---

## What the system adapter sees

The system adapter receives a `Workspace` object:

```python
class Workspace(BaseModel):
    path: Path                # absolute path to the workspace root
    metadata: dict            # e.g. {"git_before": "91b2abc"} for git workspaces
```

The adapter is free to:
- Pass `workspace.path` as `cwd` to a subprocess (`cli` adapter).
- Mount it into a docker container (`docker` adapter).
- Substitute it into a request body (`http` adapter, for systems that accept a path).

The adapter is **not** free to:
- Touch files outside `workspace.path`.
- Pre-clean or modify the workspace before the run (the snapshot was already taken).
- Skip the workspace handle.

---

## What evaluators see

Evaluators receive `FilesystemArtifact` only if the run has a workspace. They do not get `Workspace` (the live path). They get the diff.

```python
class FilesystemArtifact(BaseModel):
    case_id: str
    variant_name: str
    workspace_kind: str
    before_manifest: FileManifest
    after_manifest: FileManifest
    diff: FileDiff
    artifacts_path: str        # path under run_dir/artifacts/ where files live
```

Evaluators that need to *read* changed file contents can do so via `artifacts_path`, which is a copy made during `cleanup()` so the source workspace can be torn down.

### Artifacts ship through `ObjectStorage` (v2)

Each workspace adapter (`tempdir_snapshot`, `git`, `docker_volume`) accepts an optional `object_storage` parameter. When set, the built `FilesystemArtifact` is uploaded via the storage's `put(key, data)` call at the stable key `<case>/<variant>/artifact.json`, and `artifacts_path` is rewritten to the storage URL (`file://…`, `s3://…`, `memory://…`, etc.). The artifact's *pydantic shape* is unchanged — evaluators read `artifact.artifacts_path` the same way; only the bytes-mover differs.

The default for single-machine `evalh run` is a local `file://` storage rooted at `runs/<run_id>/artifacts/`, which preserves the existing layout — nobody has to opt in to anything. Distributed executors (v2 K8s / Modal / Ray) point `object_storage` at an `s3://` / `gs://` / `az://` URL so workers in containers can ship artifacts back to one place. The fsspec-backed `FsspecObjectStorage` handles every protocol; cloud-specific extras pull the matching fsspec backend (`[s3]` → `s3fs`, `[gcs]` → `gcsfs`, `[azure]` → `adlfs`).

---

## Filesystem-aware evaluators

### `git_diff`

```yaml
- name: changed_the_right_files
  type: git_diff
  config:
    expected_modified: ["src/pricing.py", "tests/test_pricing.py"]
    expected_added: []
    expected_removed: []
    forbidden_paths: ["README.md", ".env"]
    require_renamed_function:
      old: compute_price
      new: calculate_price
```

Compares `artifact.diff` against the rule. Pure function of the manifest; works for both `tempdir_snapshot` and `git` workspaces.

### `command`

Runs a shell command in the workspace, passes on exit code 0.

```yaml
- name: tests_pass
  type: command
  config:
    command: ["pytest", "tests/", "-x"]
    timeout_seconds: 120
    env:
      PYTHONPATH: "."
    capture_output: true        # store stdout/stderr in detail
```

Runs *inside the workspace artifact directory* (which is a copy of the post-run state). The original workspace has been torn down by the time the evaluator runs. This means:

- The system can't fight the evaluator (it's already done).
- Re-running the evaluator is deterministic given the same artifact.
- The evaluator runs in the user's interpreter, **not** sandboxed by default. v0 documents this; v1 adds `docker_volume` workspaces for sandboxing.

---

## Why this design

| Rule | Why |
|---|---|
| Trace first | Most evals don't need the filesystem; making it optional keeps the simple case simple. |
| Snapshot, not access | An evaluator that reads `~/Documents` is a security hole. The diff is a constrained interface. |
| No git default | Less environmental state, faster, works on minimal CI. |
| Same artifact shape across workspace types | Evaluators don't branch on `workspace_kind`. |
| Cleanup copies first | Lets the runner tear down workspaces aggressively without losing inspection ability. |

---

## Failure modes the design prevents

- **Cross-case bleed.** Each case gets a fresh tempdir. No shared state.
- **Workspace race conditions.** The runner creates and destroys the workspace; the system adapter never owns its lifecycle.
- **"Where did the diff go?"** The artifact directory is under the run directory, named per case × variant. Always findable.
- **Disk fill.** Cleanup is mandatory. The run summary records bytes copied vs bytes deleted; gross mismatch fails the run.
