from __future__ import annotations

import errno
import gzip
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import artifact_extract
from artifact_extract import ArtifactExtractError, extract_verified_tarball


def _tar(entries: list[tuple[str, bytes | None, bytes | None]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload, member_type in entries:
            info = tarfile.TarInfo(name)
            if member_type is not None:
                info.type = member_type
            if payload is None:
                info.size = 0
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _declared_extension_header(member_type: bytes, size: int) -> bytes:
    info = tarfile.TarInfo("metadata")
    info.type = member_type
    info.size = size
    return gzip.compress(info.tobuf() + (b"\0" * tarfile.RECORDSIZE))


def _ready(
    tmp_path: Path, payload: bytes
) -> tuple[Path, dict[str, object], Path]:
    cache = tmp_path / "cache"
    staging = tmp_path / "staging"
    cache.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    cache.chmod(0o700)
    staging.chmod(0o700)
    digest = hashlib.sha256(payload).hexdigest()
    artifact = cache / digest
    artifact.write_bytes(payload)
    artifact.chmod(0o600)
    asset = {
        "name": "agent-cockpit-server-1.2.3-linux-x86_64.tar.gz",
        "size": len(payload),
        "sha256": digest,
    }
    return artifact, asset, staging / "generation"


def _assert_code(code: str, call: object) -> None:
    with pytest.raises(ArtifactExtractError) as exc_info:
        call()  # type: ignore[operator]
    assert exc_info.value.code == code
    assert str(exc_info.value) == code


def test_extracts_happy_tree_with_fixed_modes_and_content(tmp_path: Path) -> None:
    payload = _tar([
        ("app/", None, tarfile.DIRTYPE),
        ("app/bin/", None, tarfile.DIRTYPE),
        ("app/bin/server", b"server-bytes", None),
        ("VERSION", b"1.2.3\n", None),
    ])
    artifact, asset, destination = _ready(tmp_path, payload)

    assert extract_verified_tarball(artifact, asset, destination) == destination

    assert (destination / "app/bin/server").read_bytes() == b"server-bytes"
    assert (destination / "VERSION").read_bytes() == b"1.2.3\n"
    for directory in (destination, destination / "app", destination / "app/bin"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file_path in (destination / "app/bin/server", destination / "VERSION"):
        assert stat.S_IMODE(file_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "name",
    ["/absolute", "../escape", "a/../escape", "./dot", "a\\evil", "a//evil"],
)
def test_rejects_unsafe_member_paths_before_destination(
    tmp_path: Path, name: str
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([(name, b"x", None)]))

    _assert_code(
        "archive_invalid", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert not destination.exists()


def test_rejects_duplicate_normalized_member_path(tmp_path: Path) -> None:
    artifact, asset, destination = _ready(
        tmp_path, _tar([("same", b"one", None), ("same", b"two", None)])
    )

    _assert_code(
        "archive_invalid", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert not destination.exists()


def test_rejects_nul_member_name() -> None:
    info = tarfile.TarInfo("bad\x00name")

    _assert_code("archive_invalid", lambda: artifact_extract._member_path(info))


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("a" * 256, "archive_limit_exceeded"),
        ("é" * 128, "archive_limit_exceeded"),
        ("a/" * 128 + "a", "archive_limit_exceeded"),
        ("a" * 250 + "/" + "b" * 250, "archive_limit_exceeded"),
    ],
)
def test_rejects_member_component_depth_and_total_utf8_byte_limits(
    monkeypatch: pytest.MonkeyPatch, name: str, code: str
) -> None:
    monkeypatch.setattr(artifact_extract, "MAX_MEMBER_PATH_BYTES", 400)
    info = tarfile.TarInfo(name)

    _assert_code(code, lambda: artifact_extract._member_path(info))


def test_accepts_member_path_exactly_at_all_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_extract, "MAX_MEMBER_PATH_BYTES", 6)
    monkeypatch.setattr(artifact_extract, "MAX_MEMBER_COMPONENT_BYTES", 3)
    monkeypatch.setattr(artifact_extract, "MAX_MEMBER_DEPTH", 2)

    assert artifact_extract._member_path(tarfile.TarInfo("abc/é")) == ("abc", "é")


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
        tarfile.GNUTYPE_SPARSE,
        b"Z",
    ],
)
def test_rejects_links_devices_fifo_sparse_and_unknown_types(
    tmp_path: Path, member_type: bytes
) -> None:
    artifact, asset, destination = _ready(
        tmp_path, _tar([("unsafe", None, member_type)])
    )

    _assert_code(
        "archive_invalid", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert not destination.exists()


def test_rejects_member_and_total_bomb_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _tar([("one", b"12345", None), ("two", b"12345", None)])
    artifact, asset, destination = _ready(tmp_path, payload)
    monkeypatch.setattr(artifact_extract, "MAX_MEMBER_BYTES", 4)
    _assert_code(
        "archive_limit_exceeded",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    monkeypatch.setattr(artifact_extract, "MAX_MEMBER_BYTES", 10)
    monkeypatch.setattr(artifact_extract, "MAX_EXTRACTED_BYTES", 9)
    _assert_code(
        "archive_limit_exceeded",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert not destination.exists()


def test_rejects_member_count_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact, asset, destination = _ready(
        tmp_path, _tar([("one", b"1", None), ("two", b"2", None)])
    )
    monkeypatch.setattr(artifact_extract, "MAX_MEMBERS", 1)

    _assert_code(
        "archive_limit_exceeded",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )


def test_prefix_index_rejects_file_directory_collisions_in_either_order(
    tmp_path: Path,
) -> None:
    for entries in (
        [("app", b"file", None), ("app/child", b"child", None)],
        [("app/child", b"child", None), ("app", b"file", None)],
    ):
        case = tmp_path / str(len(list(tmp_path.iterdir())))
        case.mkdir(mode=0o700)
        artifact, asset, destination = _ready(case, _tar(entries))
        _assert_code(
            "archive_invalid",
            lambda: extract_verified_tarball(artifact, asset, destination),
        )
        assert not destination.exists()


def test_prefix_index_does_not_scan_all_prior_flat_members() -> None:
    slice_count = 0

    class TrackingTuple(tuple[str, ...]):
        def __getitem__(self, key: object) -> object:
            nonlocal slice_count
            if isinstance(key, slice):
                slice_count += 1
            return super().__getitem__(key)  # type: ignore[index]

    class FakeMember:
        size = 0

        def issparse(self) -> bool:
            return False

        def isdir(self) -> bool:
            return False

        def isreg(self) -> bool:
            return True

    members = [FakeMember() for _ in range(artifact_extract.MAX_MEMBERS)]
    paths = iter(TrackingTuple((f"file-{index}",)) for index in range(len(members)))
    original = artifact_extract._member_path
    artifact_extract._member_path = lambda _member: next(paths)  # type: ignore[assignment]
    try:
        scanned = artifact_extract._scan_members(iter(members))  # type: ignore[arg-type]
    finally:
        artifact_extract._member_path = original

    assert len(scanned) == artifact_extract.MAX_MEMBERS
    assert slice_count == 0


@pytest.mark.parametrize("member_type", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME])
def test_rejects_declared_pax_and_gnu_metadata_bombs_before_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member_type: bytes
) -> None:
    monkeypatch.setattr(artifact_extract, "MAX_ARCHIVE_METADATA_BYTES", 1024)
    payload = _declared_extension_header(member_type, 1025)
    artifact, asset, destination = _ready(tmp_path, payload)

    _assert_code(
        "archive_limit_exceeded",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert not destination.exists()


@pytest.mark.parametrize("payload", [b"not gzip", b"\x1f\x8btruncated"])
def test_rejects_non_gzip_and_corrupt_gzip(tmp_path: Path, payload: bytes) -> None:
    artifact, asset, destination = _ready(tmp_path, payload)

    _assert_code(
        "archive_invalid", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert not destination.exists()


@pytest.mark.parametrize("mutation", ["relative", "name", "size", "digest"])
def test_rejects_invalid_artifact_path_or_metadata(
    tmp_path: Path, mutation: str
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    expected = "artifact_path_invalid" if mutation in {"relative", "name"} else "invalid_asset"
    if mutation == "relative":
        artifact = Path(artifact.name)
    elif mutation == "name":
        renamed = artifact.with_name("wrong-name")
        artifact.rename(renamed)
        artifact = renamed
    elif mutation == "size":
        asset["size"] = True
    else:
        asset["sha256"] = "A" * 64

    _assert_code(expected, lambda: extract_verified_tarball(artifact, asset, destination))
    assert not destination.exists()


@pytest.mark.parametrize("mutation", ["size", "digest"])
def test_rejects_artifact_content_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    if mutation == "size":
        asset["size"] = asset["size"] + 1  # type: ignore[operator]
    else:
        wrong = "0" * 64
        artifact.rename(artifact.with_name(wrong))
        artifact = artifact.with_name(wrong)
        asset["sha256"] = wrong

    code = "artifact_unsafe" if mutation == "size" else "artifact_mismatch"
    _assert_code(code, lambda: extract_verified_tarball(artifact, asset, destination))
    assert not destination.exists()


@pytest.mark.parametrize("mutation", ["mode", "hardlink", "symlink"])
def test_rejects_insecure_artifact_without_destination(
    tmp_path: Path, mutation: str
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    if mutation == "mode":
        artifact.chmod(0o644)
    elif mutation == "hardlink":
        os.link(artifact, artifact.parent / "other-link")
    else:
        real = artifact.parent / "real"
        artifact.rename(real)
        artifact.symlink_to(real)

    _assert_code("artifact_unsafe", lambda: extract_verified_tarball(artifact, asset, destination))
    assert not destination.exists()


def test_rejects_artifact_signature_change_while_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    original = artifact_extract._scan_members

    def mutate(archive: tarfile.TarFile):
        members = original(archive)
        artifact.chmod(0o644)
        return members

    monkeypatch.setattr(artifact_extract, "_scan_members", mutate)

    _assert_code("artifact_unsafe", lambda: extract_verified_tarball(artifact, asset, destination))
    assert not destination.exists()


def test_rejects_artifact_or_parent_replaced_while_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    original = artifact_extract._scan_members
    replacement = artifact.parent / "replacement"
    replacement.write_bytes(artifact.read_bytes())
    replacement.chmod(0o600)

    def replace_leaf(archive: tarfile.TarFile):
        members = original(archive)
        artifact.rename(artifact.parent / "held")
        replacement.rename(artifact)
        return members

    monkeypatch.setattr(artifact_extract, "_scan_members", replace_leaf)
    _assert_code("artifact_unsafe", lambda: extract_verified_tarball(artifact, asset, destination))
    assert not destination.exists()


def test_rejects_artifact_leaf_with_wrong_owner_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    real_uid = os.getuid()
    monkeypatch.setattr(
        artifact_extract,
        "_directory_secure",
        lambda info: (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == real_uid
            and stat.S_IMODE(info.st_mode) == 0o700
        ),
    )
    monkeypatch.setattr(artifact_extract.os, "getuid", lambda: real_uid + 1)

    _assert_code("artifact_unsafe", lambda: extract_verified_tarball(artifact, asset, destination))
    assert not destination.exists()


def test_rejects_symlink_or_unsafe_artifact_parent(tmp_path: Path) -> None:
    payload = _tar([("file", b"x", None)])
    artifact, asset, destination = _ready(tmp_path, payload)
    artifact.parent.chmod(0o755)
    _assert_code(
        "artifact_path_invalid",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )

    artifact.parent.chmod(0o700)
    real = artifact.parent
    moved = tmp_path / "moved-cache"
    real.rename(moved)
    real.symlink_to(moved, target_is_directory=True)
    artifact = real / asset["sha256"]  # type: ignore[operator]
    _assert_code(
        "artifact_path_invalid",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )


def test_rejects_destination_parent_symlink_unsafe_mode_and_existing(
    tmp_path: Path,
) -> None:
    payload = _tar([("file", b"x", None)])
    artifact, asset, destination = _ready(tmp_path, payload)
    destination.parent.chmod(0o755)
    _assert_code(
        "destination_unsafe",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    destination.parent.chmod(0o700)
    destination.mkdir()
    marker = destination / "keep"
    marker.write_text("keep")
    _assert_code(
        "destination_exists",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert marker.read_text() == "keep"

    destination.rename(tmp_path / "existing")
    real_parent = destination.parent
    moved_parent = tmp_path / "moved-staging"
    real_parent.rename(moved_parent)
    real_parent.symlink_to(moved_parent, target_is_directory=True)
    destination = real_parent / "new"
    _assert_code(
        "destination_invalid",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )


def test_existing_destination_symlink_is_not_followed_or_deleted(tmp_path: Path) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("keep")
    destination.symlink_to(outside, target_is_directory=True)

    _assert_code(
        "destination_exists",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert destination.is_symlink()
    assert marker.read_text() == "keep"


def test_rejects_destination_parent_with_wrong_owner_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    real_open = artifact_extract._open_artifact
    real_uid = os.getuid()

    def open_then_change_owner_contract(*args: object, **kwargs: object):
        result = real_open(*args, **kwargs)
        monkeypatch.setattr(artifact_extract.os, "getuid", lambda: real_uid + 1)
        return result

    monkeypatch.setattr(artifact_extract, "_open_artifact", open_then_change_owner_contract)

    _assert_code("destination_unsafe", lambda: extract_verified_tarball(artifact, asset, destination))
    assert not destination.exists()


def test_rejects_destination_leaf_over_filesystem_byte_boundary(
    tmp_path: Path,
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    destination = destination.with_name("é" * 128)

    _assert_code(
        "destination_invalid",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert destination.name not in os.listdir(destination.parent)


def test_accepts_destination_leaf_at_filesystem_byte_boundary(tmp_path: Path) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    destination = destination.with_name("a" * artifact_extract.MAX_DESTINATION_NAME_BYTES)

    assert extract_verified_tarball(artifact, asset, destination) == destination
    assert (destination / "file").read_bytes() == b"x"


def test_write_race_never_overwrites_existing_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"payload", None)]))
    real_open = artifact_extract.os.open
    raced = False

    def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal raced
        if path == "file" and flags & os.O_EXCL and not raced:
            raced = True
            (destination / "file").write_bytes(b"attacker")
            (destination / "file").chmod(0o600)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifact_extract.os, "open", racing_open)

    _assert_code(
        "extract_failed", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert raced is True
    assert (destination / "file").read_bytes() == b"attacker"


@pytest.mark.parametrize("write_errno", [errno.ENOSPC, errno.EIO])
def test_destination_write_errors_are_extract_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_errno: int
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"payload", None)]))
    monkeypatch.setattr(
        artifact_extract.os,
        "write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(write_errno, "write")),
    )

    _assert_code(
        "extract_failed", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert destination.is_dir()
    assert not (destination / "file").is_symlink()


def test_archive_source_read_error_is_archive_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"payload", None)]))
    real_extractfile = tarfile.TarFile.extractfile

    class BrokenSource:
        def read(self, _size: int = -1) -> bytes:
            raise OSError(errno.EIO, "corrupt source")

        def close(self) -> None:
            pass

    def broken_extractfile(
        archive: tarfile.TarFile, member: tarfile.TarInfo
    ) -> object:
        source = real_extractfile(archive, member)
        assert source is not None
        source.close()
        return BrokenSource()

    monkeypatch.setattr(tarfile.TarFile, "extractfile", broken_extractfile)

    _assert_code(
        "archive_invalid", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert destination.is_dir()


def test_server_artifact_uses_explicit_python_argv_not_tar_exec_mode(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        server = tarfile.TarInfo("server.py")
        server.mode = 0o755
        body = b'print("server-fixture-ok")\n'
        server.size = len(body)
        archive.addfile(server, io.BytesIO(body))
        version = tarfile.TarInfo("VERSION")
        version.size = len(b"1.2.3\n")
        archive.addfile(version, io.BytesIO(b"1.2.3\n"))
    artifact, asset, destination = _ready(tmp_path, output.getvalue())

    extract_verified_tarball(artifact, asset, destination)

    entrypoint = destination / "server.py"
    assert stat.S_IMODE(entrypoint.stat().st_mode) == 0o600
    completed = subprocess.run(
        [sys.executable, str(entrypoint)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "server-fixture-ok\n"


def test_destination_parent_replaced_while_writing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"payload", None)]))
    original = artifact_extract._write_members
    staging = destination.parent
    moved = tmp_path / "moved-staging"
    attacker = tmp_path / "attacker-staging"
    attacker.mkdir(mode=0o700)
    attacker.chmod(0o700)

    def replace_parent(*args: object, **kwargs: object):
        result = original(*args, **kwargs)
        staging.rename(moved)
        staging.symlink_to(attacker, target_is_directory=True)
        return result

    monkeypatch.setattr(artifact_extract, "_write_members", replace_parent)

    _assert_code(
        "destination_unsafe",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert (moved / "generation/file").read_bytes() == b"payload"
    assert not (attacker / "generation").exists()


def test_failure_may_leave_partial_staging_but_never_promotes_or_cleans_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(
        tmp_path, _tar([("first", b"one", None), ("second", b"two", None)])
    )
    real_write = artifact_extract._write_member_file

    def fail_second(
        archive: tarfile.TarFile,
        member: tarfile.TarInfo,
        root_fd: int,
        parts: tuple[str, ...],
        directories: dict[tuple[str, ...], tuple[int, int]],
    ) -> tuple[int, ...]:
        if parts == ("second",):
            raise ArtifactExtractError("extract_failed")
        return real_write(archive, member, root_fd, parts, directories)

    monkeypatch.setattr(artifact_extract, "_write_member_file", fail_second)

    _assert_code(
        "extract_failed", lambda: extract_verified_tarball(artifact, asset, destination)
    )
    assert (destination / "first").read_bytes() == b"one"
    assert not (destination / "second").exists()


def test_unexpected_internal_error_is_stable_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(tmp_path, _tar([("file", b"x", None)]))
    monkeypatch.setattr(
        artifact_extract,
        "_scan_members",
        lambda _archive: (_ for _ in ()).throw(RuntimeError("secret path")),
    )

    _assert_code(
        "extract_failed",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert not destination.exists()


def test_nested_directory_replaced_while_writing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, asset, destination = _ready(
        tmp_path, _tar([("app/one", b"one", None), ("app/two", b"two", None)])
    )
    real_write = artifact_extract._write_member_file
    replaced = False

    def replace_after_first(
        archive: tarfile.TarFile,
        member: tarfile.TarInfo,
        root_fd: int,
        parts: tuple[str, ...],
        directories: dict[tuple[str, ...], tuple[int, int]],
    ) -> tuple[int, ...]:
        nonlocal replaced
        result = real_write(archive, member, root_fd, parts, directories)
        if parts == ("app", "one"):
            (destination / "app").rename(destination / "held-app")
            (destination / "app").mkdir(mode=0o700)
            (destination / "app").chmod(0o700)
            replaced = True
        return result

    monkeypatch.setattr(artifact_extract, "_write_member_file", replace_after_first)

    _assert_code(
        "destination_unsafe",
        lambda: extract_verified_tarball(artifact, asset, destination),
    )
    assert replaced is True
    assert (destination / "held-app/one").read_bytes() == b"one"
    assert not (destination / "app/two").exists()
