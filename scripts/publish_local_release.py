#!/usr/bin/env python3
"""Build, sign, verify, and publish one native GitHub Release locally."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_cockpit import artifact_extract  # noqa: E402
from agent_cockpit.release_index import verify_release_index  # noqa: E402
from agent_cockpit.version import format_semver, parse_semver  # noqa: E402


REPOSITORY = "fyc0451/agent-cockpit"
CONFIG_DIR = Path.home() / ".config" / "agent-cockpit"
PRIVATE_KEY = CONFIG_DIR / "server-release-ed25519.key"
PUBLIC_KEY = CONFIG_DIR / "server-release-ed25519.pub"
STATE_ROOT = Path.home() / ".local" / "state" / "agent-cockpit" / "local-releases"
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class LocalReleaseError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise LocalReleaseError(code)


def _run(
    command: list[str],
    *,
    cwd: Path,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=check,
    )


def _output(command: list[str], *, cwd: Path) -> str:
    try:
        return _run(command, cwd=cwd).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise LocalReleaseError("command_failed") from exc


def _private_file(path: Path, size: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise LocalReleaseError("release_key_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_size != size
    ):
        _fail("release_key_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LocalReleaseError("release_key_unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
             opened.st_nlink, opened.st_size, opened.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                before.st_nlink, before.st_size, before.st_mtime_ns)
        ):
            _fail("release_key_unsafe")
        payload = os.read(fd, size + 1)
        if len(payload) != size or os.read(fd, 1):
            _fail("release_key_unsafe")
        after = os.fstat(fd)
        if (
            (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
             after.st_nlink, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
                opened.st_nlink, opened.st_size, opened.st_mtime_ns)
        ):
            _fail("release_key_unsafe")
        return payload
    finally:
        os.close(fd)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise LocalReleaseError("release_state_unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        _fail("release_state_unsafe")


def _validate_source(source: Path, candidate: str) -> tuple[str, str, int]:
    if _SHA_RE.fullmatch(candidate) is None:
        _fail("candidate_invalid")
    if _output(["git", "rev-parse", "HEAD"], cwd=source) != candidate:
        _fail("candidate_mismatch")
    if _output(["git", "status", "--porcelain"], cwd=source):
        _fail("source_dirty")
    if _output(["git", "ls-remote", "origin", "refs/heads/main"], cwd=source).split()[0] != candidate:
        _fail("remote_main_mismatch")
    raw_version = (source / "VERSION").read_text(encoding="ascii")
    if not raw_version.endswith("\n") or raw_version.count("\n") != 1:
        _fail("version_invalid")
    version = raw_version[:-1]
    parsed = parse_semver(version)
    if parsed is None or version != format_semver(parsed):
        _fail("version_invalid")
    tag = f"agent-cockpit-v{version}"
    epoch_raw = _output(["git", "show", "-s", "--format=%ct", candidate], cwd=source)
    if not epoch_raw.isdecimal():
        _fail("source_epoch_invalid")
    return version, tag, int(epoch_raw)


def _release_absent(tag: str, source: Path) -> None:
    result = _run(
        ["gh", "release", "view", tag, "-R", REPOSITORY],
        cwd=source,
        check=False,
    )
    if result.returncode == 0:
        _fail("release_exists")
    if "release not found" not in (result.stderr or "").lower():
        _fail("release_check_failed")
    remote_tag = _output(
        ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], cwd=source,
    )
    if remote_tag:
        _fail("tag_exists")


def _verify_assets(
    root: Path,
    *,
    archive_name: str,
    public_key: bytes,
    verify_root: Path,
) -> dict[str, Any]:
    names = {path.name for path in root.iterdir() if path.is_file()}
    expected = {archive_name, "release-index.json", "release-index.json.sig"}
    if names != expected:
        _fail("asset_set_invalid")
    index = (root / "release-index.json").read_bytes()
    signature = (root / "release-index.json.sig").read_bytes()
    verified = verify_release_index(
        index, signature, public_key, platform="linux", arch="x86_64",
    )
    selected = verified["selected_asset"]
    if selected["name"] != archive_name:
        _fail("asset_name_mismatch")
    archive = root / archive_name
    if (
        archive.stat().st_size != selected["size"]
        or hashlib.sha256(archive.read_bytes()).hexdigest() != selected["sha256"]
    ):
        _fail("asset_digest_mismatch")
    cache = verify_root / "cache"
    staging = verify_root / "staging"
    cache.mkdir(mode=0o700, parents=True)
    staging.mkdir(mode=0o700)
    cached = cache / selected["sha256"]
    shutil.copyfile(archive, cached)
    cached.chmod(0o600)
    generation = artifact_extract.extract_verified_tarball(
        cached, selected, staging / "generation",
    )
    artifact_extract.verify_server_launcher(generation, selected["launcher"])
    return verified


def publish(source: Path, candidate: str, release_id: str) -> dict[str, Any]:
    if _RELEASE_ID_RE.fullmatch(release_id) is None:
        _fail("release_id_invalid")
    source = source.resolve(strict=True)
    version, tag, source_date_epoch = _validate_source(source, candidate)
    _release_absent(tag, source)
    private = _private_file(PRIVATE_KEY, 32)
    public = _private_file(PUBLIC_KEY, 32)
    key = Ed25519PrivateKey.from_private_bytes(private)
    derived_public = key.public_key().public_bytes_raw()
    if derived_public != public:
        _fail("release_key_mismatch")

    _private_directory(STATE_ROOT)
    state = STATE_ROOT / release_id
    try:
        state.mkdir(mode=0o700)
    except FileExistsError:
        _fail("release_state_exists")
    assets = state / "assets"
    assets.mkdir(mode=0o700)
    archive_name = f"agent-cockpit-server-{version}-linux-x86_64.tar.gz"
    receipt = {
        "candidate": candidate,
        "release_id": release_id,
        "state": "building",
        "tag": tag,
        "version": version,
    }
    _write_json(state / "receipt.json", receipt)

    try:
        _run(
            [
                sys.executable,
                str(source / "scripts" / "build_server_artifact.py"),
                "--source-root", str(source),
                "--output-dir", str(assets),
                "--source-sha", candidate,
                "--source-date-epoch", str(source_date_epoch),
            ],
            cwd=source,
            capture=False,
        )
        index = (assets / "release-index.json").read_bytes()
        signature = key.sign(index)
        if len(signature) != 64:
            _fail("signature_invalid")
        signature_path = assets / "release-index.json.sig"
        signature_path.write_bytes(signature)
        signature_path.chmod(0o600)
        verified = _verify_assets(
            assets,
            archive_name=archive_name,
            public_key=public,
            verify_root=state / "local-verify",
        )
        if verified["source_sha"] != candidate or verified["version"] != version:
            _fail("release_identity_mismatch")

        title = f"{tag} (local {release_id})"
        _run(
            [
                "gh", "release", "create", tag,
                str(assets / archive_name),
                str(assets / "release-index.json"),
                str(signature_path),
                "--draft", "--target", candidate,
                "--title", title,
                "--notes", f"Signed native server release for {candidate}.",
                "-R", REPOSITORY,
            ],
            cwd=source,
            capture=False,
        )
        receipt["state"] = "draft_created"
        _write_json(state / "receipt.json", receipt)

        remote = json.loads(_output(
            [
                "gh", "release", "view", tag, "-R", REPOSITORY,
                "--json", "isDraft,isPrerelease,tagName,name,assets",
            ],
            cwd=source,
        ))
        remote_names = [asset["name"] for asset in remote.get("assets", [])]
        if (
            remote.get("tagName") != tag
            or remote.get("name") != title
            or remote.get("isDraft") is not True
            or remote.get("isPrerelease") is not False
            or len(remote_names) != 3
            or set(remote_names) != {archive_name, "release-index.json", "release-index.json.sig"}
        ):
            _fail("remote_draft_invalid")

        remote_assets = state / "remote-assets"
        remote_assets.mkdir(mode=0o700)
        _run(
            ["gh", "release", "download", tag, "-R", REPOSITORY, "-D", str(remote_assets)],
            cwd=source,
            capture=False,
        )
        for name in (archive_name, "release-index.json", "release-index.json.sig"):
            if (remote_assets / name).read_bytes() != (assets / name).read_bytes():
                _fail("remote_asset_mismatch")
        _verify_assets(
            remote_assets,
            archive_name=archive_name,
            public_key=public,
            verify_root=state / "remote-verify",
        )

        release_api = json.loads(_output(
            ["gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"], cwd=source,
        ))
        release_numeric_id = release_api.get("id")
        if type(release_numeric_id) is not int or release_api.get("draft") is not True:
            _fail("remote_draft_invalid")
        published = _output(
            [
                "gh", "api", "--method", "PATCH",
                f"repos/{REPOSITORY}/releases/{release_numeric_id}",
                "-F", "draft=false", "-f", "make_latest=true",
                "--jq", ".draft == false",
            ],
            cwd=source,
        )
        if published != "true":
            _fail("release_publish_failed")
        remote_tag = _output(
            ["git", "ls-remote", "origin", f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"],
            cwd=source,
        )
        resolved = {line.split()[0] for line in remote_tag.splitlines() if line.strip()}
        if candidate not in resolved:
            _fail("remote_tag_mismatch")
        receipt.update({
            "asset_sha256": verified["selected_asset"]["sha256"],
            "public_key_sha256": hashlib.sha256(public).hexdigest(),
            "state": "published",
        })
        _write_json(state / "receipt.json", receipt)
        return receipt
    except Exception as exc:
        receipt["state"] = "failed"
        receipt["error_code"] = (
            str(exc) if isinstance(exc, LocalReleaseError) else "release_failed"
        )
        _write_json(state / "receipt.json", receipt)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        receipt = publish(args.source_root, args.candidate, args.release_id)
    except (LocalReleaseError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, LocalReleaseError) else "release_failed"
        print(f"LOCAL_RELEASE_BLOCK: {code}", file=sys.stderr)
        return 1
    print(
        f"LOCAL_RELEASE_OK tag={receipt['tag']} candidate={receipt['candidate']} "
        f"state={receipt['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
