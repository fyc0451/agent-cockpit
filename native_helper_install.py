"""Install release-external stable entrypoints for native helper commands."""
from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path


HELPER_COMMANDS = (
    "am-register",
    "am-retire",
    "am-init-project",
    "mail-send",
    "mail-recv",
    "mail-identity-inject",
    "task-report",
)
HELPER_TARGET = "../current/bin/agent-cockpit"
RECEIPT_NAME = ".agent-cockpit-ownership.json"
_RECEIPT_KEYS = frozenset({"schema_version", "owner", "links"})


class HelperInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class HelperInstallResult:
    managed: tuple[str, ...]
    preserved: tuple[str, ...]
    receipt: Path


def _validate_deploy_root(deploy_root: Path) -> None:
    if not deploy_root.is_absolute():
        raise HelperInstallError("deploy_root_invalid")
    try:
        info = deploy_root.lstat()
    except OSError as exc:
        raise HelperInstallError("deploy_root_invalid") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise HelperInstallError("deploy_root_invalid")


def _prepare_helpers(deploy_root: Path) -> Path:
    helpers = deploy_root / "helpers"
    try:
        helpers.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise HelperInstallError("helpers_invalid") from exc
    try:
        info = helpers.lstat()
    except OSError as exc:
        raise HelperInstallError("helpers_invalid") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise HelperInstallError("helpers_invalid")
    return helpers


def _load_receipt(path: Path) -> dict[str, str]:
    if not os.path.lexists(path):
        return {}
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise HelperInstallError("receipt_invalid")
        value = json.loads(path.read_text(encoding="ascii"))
    except HelperInstallError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise HelperInstallError("receipt_invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != _RECEIPT_KEYS
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("owner") != "agent-cockpit"
        or not isinstance(value.get("links"), dict)
    ):
        raise HelperInstallError("receipt_invalid")
    links = value["links"]
    if any(
        command not in HELPER_COMMANDS
        or not isinstance(target, str)
        or not target
        or "\x00" in target
        or Path(target).is_absolute()
        for command, target in links.items()
    ):
        raise HelperInstallError("receipt_invalid")
    return dict(links)


def _replace_link(path: Path, target: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _write_receipt(path: Path, links: dict[str, str]) -> None:
    payload = {
        "schema_version": 1,
        "owner": "agent-cockpit",
        "links": links,
    }
    fd, temporary = tempfile.mkstemp(prefix=".ownership-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def install_helper_links(deploy_root: Path) -> HelperInstallResult:
    deploy_root = Path(deploy_root)
    _validate_deploy_root(deploy_root)
    helpers = _prepare_helpers(deploy_root)
    receipt_path = helpers / RECEIPT_NAME
    owned = _load_receipt(receipt_path)
    managed: list[str] = []
    preserved: list[str] = []
    changed: list[tuple[Path, str | None]] = []

    try:
        for command in HELPER_COMMANDS:
            path = helpers / command
            previous_target = owned.get(command)
            if os.path.lexists(path):
                if (
                    path.is_symlink()
                    and previous_target is not None
                    and os.readlink(path) == previous_target
                ):
                    if previous_target != HELPER_TARGET:
                        _replace_link(path, HELPER_TARGET)
                        changed.append((path, previous_target))
                    managed.append(command)
                else:
                    preserved.append(command)
                continue
            _replace_link(path, HELPER_TARGET)
            changed.append((path, None))
            managed.append(command)

        new_links = {command: HELPER_TARGET for command in managed}
        _write_receipt(receipt_path, new_links)
    except BaseException as exc:
        for path, old_target in reversed(changed):
            try:
                if old_target is None:
                    if path.is_symlink() and os.readlink(path) == HELPER_TARGET:
                        path.unlink()
                else:
                    _replace_link(path, old_target)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise HelperInstallError("install_failed") from exc
        raise

    return HelperInstallResult(tuple(managed), tuple(preserved), receipt_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = install_helper_links(args.deploy_root)
    except HelperInstallError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "managed": list(result.managed),
        "preserved": list(result.preserved),
        "receipt": str(result.receipt),
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
