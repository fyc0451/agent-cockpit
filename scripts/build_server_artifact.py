#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_index import SERVER_LAUNCHER_PATH, canonical_bytes  # noqa: E402
from store_schema import required_manifest_digest_paths  # noqa: E402
from version import format_semver, parse_semver  # noqa: E402


_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_ONEDIR_CHILDREN = frozenset({"agent-cockpit", "_internal"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_tree(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("tree_invalid")
    paths: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("tree_invalid")
        info = path.stat(follow_symlinks=False)
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("tree_invalid")
        paths.append(path)
    return paths


def _copy_tree(source: Path, destination: Path) -> None:
    paths = _regular_tree(source)
    destination.mkdir(mode=0o700)
    for path in sorted(paths, key=lambda item: item.relative_to(source).as_posix().encode()):
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(mode=0o700)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _canonical_version(source_root: Path) -> str:
    path = source_root / "VERSION"
    if path.is_symlink() or not path.is_file():
        raise ValueError("version_invalid")
    raw = path.read_text(encoding="ascii").strip()
    parsed = parse_semver(raw)
    if parsed is None or raw != format_semver(parsed):
        raise ValueError("version_invalid")
    return raw


def _require_elf_x86_64(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("launcher_invalid")
    with path.open("rb") as stream:
        header = stream.read(64)
    if (
        len(header) < 20
        or header[:4] != b"\x7fELF"
        or header[4] != 2
        or header[5] != 1
        or header[6] != 1
        or int.from_bytes(header[18:20], "little") != 62
    ):
        raise ValueError("launcher_invalid")
    return path.stat().st_size, _sha256(path)


def _normalize_modes(root: Path) -> None:
    for path in _regular_tree(root):
        path.chmod(0o700 if path.is_dir() else 0o600)
    (root / SERVER_LAUNCHER_PATH).chmod(0o700)
    root.chmod(0o700)


def assemble_generation(
    source_root: Path,
    onedir_root: Path,
    generation_root: Path,
    source_sha: str,
) -> Path:
    source_root = source_root.resolve(strict=True)
    onedir_root = onedir_root.resolve(strict=True)
    if not _SOURCE_SHA_RE.fullmatch(source_sha):
        raise ValueError("source_sha_invalid")
    if generation_root.exists() or generation_root.is_symlink():
        raise ValueError("generation_exists")
    if {path.name for path in onedir_root.iterdir()} != _ONEDIR_CHILDREN:
        raise ValueError("onedir_layout_invalid")
    if not (onedir_root / "_internal").is_dir():
        raise ValueError("onedir_layout_invalid")
    if not any((onedir_root / "_internal").iterdir()):
        raise ValueError("onedir_layout_invalid")
    _require_elf_x86_64(onedir_root / "agent-cockpit")
    _regular_tree(onedir_root)

    version = _canonical_version(source_root)
    static = source_root / "static"
    _regular_tree(static)

    generation_root.mkdir(mode=0o700, parents=False)
    _copy_tree(onedir_root, generation_root / "bin")
    shutil.copyfile(source_root / "VERSION", generation_root / "VERSION")
    _copy_tree(static, generation_root / "static")

    digests = {
        rel: _sha256(generation_root / rel)
        for rel in required_manifest_digest_paths(generation_root)
    }
    manifest = {
        "version": version,
        "source_sha": source_sha,
        "edition": "server",
        "digests": digests,
    }
    (generation_root / "release-manifest.json").write_bytes(
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    _normalize_modes(generation_root)
    return generation_root


def write_deterministic_tar(
    generation_root: Path,
    destination: Path,
    *,
    source_date_epoch: int,
) -> Path:
    generation_root = generation_root.resolve(strict=True)
    if source_date_epoch < 0:
        raise ValueError("source_date_epoch_invalid")
    paths = _regular_tree(generation_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as raw:
        with gzip.GzipFile(
            filename="", fileobj=raw, mode="wb", compresslevel=9,
            mtime=source_date_epoch,
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(
                    paths,
                    key=lambda item: item.relative_to(generation_root).as_posix().encode(),
                ):
                    relative = path.relative_to(generation_root).as_posix()
                    info = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = source_date_epoch
                    if path.is_dir():
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o700
                        info.size = 0
                        archive.addfile(info)
                    else:
                        info.type = tarfile.REGTYPE
                        info.mode = 0o600
                        info.size = path.stat().st_size
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
    return destination


def write_release_index(
    generation_root: Path,
    archive_path: Path,
    index_path: Path,
    source_sha: str,
) -> dict[str, Any]:
    if not _SOURCE_SHA_RE.fullmatch(source_sha):
        raise ValueError("source_sha_invalid")
    version = _canonical_version(generation_root)
    asset_name = f"agent-cockpit-server-{version}-linux-x86_64.tar.gz"
    if archive_path.name != asset_name or not archive_path.is_file():
        raise ValueError("archive_name_invalid")
    launcher = generation_root / SERVER_LAUNCHER_PATH
    launcher_size, launcher_digest = _require_elf_x86_64(launcher)
    index: dict[str, Any] = {
        "schema_version": 2,
        "tag": f"agent-cockpit-v{version}",
        "version": version,
        "source_sha": source_sha,
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": asset_name,
            "edition": "server",
            "platform": "linux",
            "arch": "x86_64",
            "size": archive_path.stat().st_size,
            "sha256": _sha256(archive_path),
            "launcher": {
                "path": SERVER_LAUNCHER_PATH,
                "size": launcher_size,
                "sha256": launcher_digest,
                "format": "elf",
            },
        }],
    }
    index_path.write_bytes(canonical_bytes(index))
    return index


def build_onedir(source_root: Path, output_root: Path) -> Path:
    spec = source_root / "packaging" / "agent-cockpit-server.spec"
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            "--distpath", str(output_root / "dist"),
            "--workpath", str(output_root / "work"), str(spec),
        ],
        cwd=source_root,
        check=True,
    )
    return output_root / "dist" / "agent-cockpit"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--onedir", type=Path)
    args = parser.parse_args()

    source_root = args.source_root.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    version = _canonical_version(source_root)
    archive = output_dir / f"agent-cockpit-server-{version}-linux-x86_64.tar.gz"
    index = output_dir / "release-index.json"
    if archive.exists() or index.exists():
        raise SystemExit("output_exists")

    with tempfile.TemporaryDirectory(prefix="agent-cockpit-server-build-") as temporary:
        work = Path(temporary)
        onedir = args.onedir.resolve(strict=True) if args.onedir else build_onedir(source_root, work)
        generation = assemble_generation(
            source_root, onedir, work / "generation", args.source_sha,
        )
        write_deterministic_tar(
            generation, archive, source_date_epoch=args.source_date_epoch,
        )
        release = write_release_index(
            generation, archive, index, args.source_sha,
        )
    print(json.dumps({
        "archive": str(archive),
        "archive_sha256": release["assets"][0]["sha256"],
        "index": str(index),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
