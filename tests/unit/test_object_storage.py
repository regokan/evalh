"""Tests for the v2 ObjectStorage abstraction.

Parametrized across `memory://` and `file://` — both fsspec backends ship
with the core install. `s3://` runs via `@pytest.mark.s3` against `moto`
when that extra is installed; otherwise the test is skipped (`moto[s3]`
is not in the core test deps).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from eval_harness.adapters.workspace.tempdir_snapshot_adapter import (
    TempdirSnapshotAdapter,
)
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, RunVariant
from eval_harness.core.object_storage import (
    FsspecObjectStorage,
    get_default_object_storage,
    object_storage_registry,
)
from eval_harness.core.object_storage.base import ObjectStorage

# ---- factory ---------------------------------------------------------------


def test_factory_registers_fsspec() -> None:
    object_storage_registry.load_entry_points()
    assert "fsspec" in object_storage_registry.names()


def test_factory_builds_from_config(tmp_path: Path) -> None:
    object_storage_registry.load_entry_points()
    inst = object_storage_registry.build(
        {"type": "fsspec", "url": tmp_path.resolve().as_uri()}
    )
    assert isinstance(inst, FsspecObjectStorage)


def test_fsspec_requires_url() -> None:
    with pytest.raises(ConfigError, match="url"):
        FsspecObjectStorage()


def test_unknown_type_raises() -> None:
    object_storage_registry.load_entry_points()
    with pytest.raises(ConfigError, match="Unknown object_storage"):
        object_storage_registry.build({"type": "made-up-name"})


def test_factory_missing_type_raises() -> None:
    with pytest.raises(ConfigError, match="missing 'type'"):
        object_storage_registry.build({})


# ---- parametrized roundtrip (memory:// + file://) --------------------------


@pytest.fixture
def memory_storage() -> Iterator[FsspecObjectStorage]:
    import uuid

    # Unique-per-test bucket so tests don't leak data into one another.
    url = f"memory://evalh-test-{uuid.uuid4().hex}"
    s = FsspecObjectStorage(url=url)
    yield s
    # Drop the bucket — fsspec's MemoryFileSystem holds state at class level.
    import fsspec

    fs = fsspec.filesystem("memory")
    for p in list(fs.store):  # type: ignore[attr-defined]
        if p.startswith(url[len("memory://"):]):
            fs.store.pop(p, None)  # type: ignore[attr-defined]


@pytest.fixture
def file_storage(tmp_path: Path) -> FsspecObjectStorage:
    return FsspecObjectStorage(url=tmp_path.resolve().as_uri())


# Both fixtures resolve to a fresh FsspecObjectStorage; the test exercises
# both via indirect parametrization on the fixture name.
@pytest.fixture(params=["memory_storage", "file_storage"])
def storage(request: pytest.FixtureRequest) -> ObjectStorage:
    return request.getfixturevalue(request.param)  # type: ignore[no-any-return]


async def test_put_get_roundtrip(storage: ObjectStorage) -> None:
    async with storage:
        url = await storage.put("hello.txt", b"world")
        assert url.endswith("/hello.txt")
        assert await storage.get("hello.txt") == b"world"


async def test_put_creates_intermediate_dirs(storage: ObjectStorage) -> None:
    async with storage:
        await storage.put("a/b/c/leaf.txt", b"x")
        assert await storage.get("a/b/c/leaf.txt") == b"x"


async def test_exists(storage: ObjectStorage) -> None:
    async with storage:
        assert await storage.exists("not_there.txt") is False
        await storage.put("there.txt", b"x")
        assert await storage.exists("there.txt") is True


async def test_list_prefix(storage: ObjectStorage) -> None:
    async with storage:
        await storage.put("group/one.txt", b"1")
        await storage.put("group/two.txt", b"2")
        await storage.put("other/three.txt", b"3")
        entries = await storage.list_prefix("group")
        # All entries land under the same prefix.
        assert len(entries) == 2
        assert all("group" in e for e in entries)
        # Names appear (regardless of full URL shape).
        joined = " ".join(entries)
        assert "one.txt" in joined
        assert "two.txt" in joined


async def test_overwrite_replaces_contents(storage: ObjectStorage) -> None:
    async with storage:
        await storage.put("k.txt", b"v1")
        await storage.put("k.txt", b"v2")
        assert await storage.get("k.txt") == b"v2"


# ---- workspace adapter integration -----------------------------------------


async def test_workspace_adapter_writes_via_object_storage(
    tmp_path: Path, memory_storage: FsspecObjectStorage
) -> None:
    """The headline contract: FilesystemArtifact's *shape* is unchanged,
    only the bytes-mover differs. The artifact JSON lands at the
    `<case>/<variant>/artifact.json` key inside the configured storage,
    and `artifact.artifacts_path` carries the storage URL."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a.txt").write_text("original\n")
    adapter = TempdirSnapshotAdapter(
        copy_from=str(fixture), object_storage=memory_storage
    )
    case = EvalCase(id="c1", input={})
    variant = RunVariant(name="v1", adapter="x", config={})
    async with memory_storage:
        ws = await adapter.prepare(case, variant)
        try:
            (ws.path / "a.txt").write_text("changed\n")
            artifact = await adapter.collect_artifacts(ws)
        finally:
            await adapter.cleanup(ws)

        # Artifact's pydantic shape unchanged.
        assert artifact.workspace_kind == "tempdir_snapshot"
        assert artifact.diff.modified == ["a.txt"]
        # Bytes landed in the storage.
        assert artifact.artifacts_path.startswith("memory://")
        assert artifact.artifacts_path.endswith("c1/v1/artifact.json")
        # Read it back through the storage Protocol.
        payload = await memory_storage.get("c1/v1/artifact.json")
        assert b"tempdir_snapshot" in payload
        assert b'"a.txt"' in payload


async def test_workspace_adapter_without_storage_keeps_local_default(
    tmp_path: Path,
) -> None:
    """No object_storage configured → existing behaviour: artifacts_path
    is the on-disk workspace path."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a.txt").write_text("original\n")
    adapter = TempdirSnapshotAdapter(copy_from=str(fixture))
    case = EvalCase(id="c1", input={})
    variant = RunVariant(name="v1", adapter="x", config={})
    ws = await adapter.prepare(case, variant)
    try:
        artifact = await adapter.collect_artifacts(ws)
    finally:
        await adapter.cleanup(ws)
    assert not artifact.artifacts_path.startswith("memory://")
    assert not artifact.artifacts_path.startswith("s3://")


# ---- default helper --------------------------------------------------------


def test_get_default_object_storage_uses_file_protocol(tmp_path: Path) -> None:
    """Local default: single-machine `evalh run` gets a file:// storage
    rooted at the run dir, preserving existing runs/ semantics."""
    s = get_default_object_storage(tmp_path)
    assert isinstance(s, FsspecObjectStorage)
    assert s.url.startswith("file://")
    assert s.scheme == "file"


# ---- s3 (gated) ------------------------------------------------------------


@pytest.mark.s3
async def test_put_get_roundtrip_s3() -> None:
    """Exercises FsspecObjectStorage against ``s3://`` via a moto server.

    Uses ``moto.server.ThreadedMotoServer`` (an actual HTTP server) rather
    than the in-process ``@mock_aws`` decorator because ``s3fs`` calls
    boto via ``aiobotocore`` and the in-process mock doesn't intercept
    those calls. The server approach proxies real-shape HTTP requests so
    s3fs can't tell the difference from real S3.

    Skipped when moto + s3fs aren't installed — the ``s3`` pytest marker
    is the gate.
    """
    pytest.importorskip("s3fs")
    moto_server = pytest.importorskip("moto.server")
    pytest.importorskip("boto3")
    import os

    import boto3

    server = moto_server.ThreadedMotoServer(port=0)
    server.start()
    try:
        host = server._server.server_address  # type: ignore[attr-defined]
        endpoint_url = f"http://{host[0]}:{host[1]}"
        # Point s3fs at moto via env vars; force-fake creds for the same
        # reason.
        os.environ["AWS_ACCESS_KEY_ID"] = "test"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

        s3 = boto3.client(
            "s3",
            region_name="us-east-1",
            endpoint_url=endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        s3.create_bucket(Bucket="evalh-test")

        storage = FsspecObjectStorage(
            url="s3://evalh-test/prefix",
            credentials={"client_kwargs": {"endpoint_url": endpoint_url}},
        )
        async with storage:
            url = await storage.put("k.txt", b"hello")
            assert url == "s3://evalh-test/prefix/k.txt"
            assert await storage.exists("k.txt") is True
            assert await storage.get("k.txt") == b"hello"
            listing = await storage.list_prefix("")
            assert any("k.txt" in p for p in listing)
    finally:
        server.stop()
