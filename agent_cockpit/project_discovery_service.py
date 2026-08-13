"""Read-only Local filesystem and Git discovery for Project registration."""
from __future__ import annotations

import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from . import project_discovery as contract


MAX_PATH_LENGTH = 4096
MAX_QUERY_LENGTH = 128
MAX_GIT_OUTPUT = 1024 * 1024
GIT_TIMEOUT_SECONDS = 5.0
MAX_REFS = 4096
MAX_HISTORY_ROOTS = 1024
MAX_POSSIBLE_PROJECTS = 100
_ROOT_ID_RE = re.compile(r"^root_[0-9a-f]{24}$")
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_FD_PATH_RE = re.compile(r"^/proc/self/fd/([0-9]+)$")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class RootReader(Protocol):
    def local_roots(self) -> Sequence[Path]: ...


class RegistryMatchReader(Protocol):
    def match_discovery(
        self,
        *,
        node_id: str,
        canonical_path: str,
        repository_fingerprint: str | None,
    ) -> tuple[
        contract.RegistryMatch | None,
        Sequence[contract.RegistryMatch],
    ]: ...


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout: float,
        max_output: int,
    ) -> CommandResult: ...


class FilesRootReader:
    """Read the existing allowlist without mutating custom-root state."""

    def local_roots(self) -> tuple[Path, ...]:
        from . import files

        groups = files.allowed_root_groups()
        return tuple(Path(value) for values in groups.values() for value in values)


class EmptyRegistryMatchReader:
    """Fail closed until the Registry read adapter is wired by the API car."""

    def match_discovery(
        self,
        *,
        node_id: str,
        canonical_path: str,
        repository_fingerprint: str | None,
    ) -> tuple[None, tuple[()]]:
        raise contract.DiscoveryError("discovery_unavailable")


class SubprocessCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str],
        timeout: float,
        max_output: int,
    ) -> CommandResult:
        if timeout <= 0 or max_output <= 0:
            raise contract.DiscoveryError("discovery_unavailable")
        try:
            pass_fds = tuple(sorted({
                int(match.group(1))
                for value in argv
                if (match := _FD_PATH_RE.fullmatch(str(value))) is not None
            }))
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(environment),
                bufsize=0,
                close_fds=True,
                pass_fds=pass_fds,
                start_new_session=True,
            )
        except OSError:
            raise contract.DiscoveryError("discovery_unavailable") from None
        try:
            stdout_bytes, stderr_bytes = _read_process_bounded(
                process, timeout=timeout, max_output=max_output
            )
        except (OSError, subprocess.TimeoutExpired, _OutputLimitExceeded):
            _kill_and_reap(process)
            raise contract.DiscoveryError("discovery_unavailable") from None
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        try:
            stdout = stdout_bytes.decode("utf-8", errors="strict")
            stderr = stderr_bytes.decode("utf-8", errors="strict")
        except UnicodeError:
            raise contract.DiscoveryError("discovery_unavailable") from None
        return CommandResult(process.returncode, stdout, stderr)


class _OutputLimitExceeded(RuntimeError):
    pass


def _read_process_bounded(
    process: subprocess.Popen[bytes], *, timeout: float, max_output: int
) -> tuple[bytes, bytes]:
    assert process.stdout is not None and process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in events:
                name = key.data
                used = len(buffers["stdout"]) + len(buffers["stderr"])
                remaining_capacity = max_output + 1 - used
                try:
                    chunk = os.read(key.fileobj.fileno(), min(65536, remaining_capacity))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[name].extend(chunk)
                if len(buffers["stdout"]) + len(buffers["stderr"]) > max_output:
                    raise _OutputLimitExceeded(name)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        process.wait(timeout=remaining)
        return bytes(buffers["stdout"]), bytes(buffers["stderr"])
    finally:
        selector.close()


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1.0)
    except (ChildProcessError, subprocess.TimeoutExpired):
        pass


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int

    def fingerprint_dict(self) -> dict[str, int]:
        return {"device": self.device, "inode": self.inode, "mode": self.mode}


@dataclass(frozen=True)
class _Root:
    descriptor: contract.RootDescriptor
    path: Path
    identity: _Identity


class LocalProjectDiscoveryService:
    def __init__(
        self,
        root_reader: RootReader | None = None,
        registry_match_reader: RegistryMatchReader | None = None,
        command_runner: CommandRunner | None = None,
    ):
        self._root_reader = root_reader or FilesRootReader()
        self._registry = registry_match_reader or EmptyRegistryMatchReader()
        self._runner = command_runner or SubprocessCommandRunner()

    def list_roots(self) -> tuple[contract.RootDescriptor, ...]:
        return _sanitized_call(
            lambda: tuple(root.descriptor for root in self._roots().values())
        )

    def list_directories(
        self,
        locator: contract.ProjectLocator,
        query: str | None = None,
    ) -> contract.DirectoryListing:
        return _sanitized_call(lambda: self._list_directories(locator, query))

    def _list_directories(
        self,
        locator: contract.ProjectLocator,
        query: str | None,
    ) -> contract.DirectoryListing:
        opened = self._open_locator(locator, allow_empty=True)
        root, target, root_identity, target_identity, root_fd, target_fd = opened
        if query is not None:
            if (
                not isinstance(query, str)
                or len(query) > MAX_QUERY_LENGTH
                or _has_control(query)
            ):
                raise contract.DiscoveryError("invalid_locator")
            needle = query.strip().casefold()
        else:
            needle = ""
        candidates: list[tuple[contract.DirectoryEntry, Path, _Identity, int]] = []
        try:
            with os.scandir(target_fd) as iterator:
                for entry in iterator:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                    if needle and needle not in entry.name.casefold():
                        continue
                    relative = (PurePosixPath(locator.path) / entry.name).as_posix()
                    if locator.path == "":
                        relative = entry.name
                    child_fd = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=target_fd)
                    canonical_path = target / entry.name
                    candidates.append((
                        contract.DirectoryEntry(
                            name=entry.name,
                            path=relative,
                            vcs_hint=_vcs_hint_at(target_fd, entry.name),
                        ),
                        canonical_path,
                        _identity_fd(child_fd),
                        child_fd,
                    ))
        except (OSError, ValueError):
            for _entry, _path, _identity_value, child_fd in candidates:
                os.close(child_fd)
            os.close(target_fd)
            os.close(root_fd)
            raise contract.DiscoveryError("discovery_unavailable") from None
        self._require_unchanged(
            locator, root, target, root_identity, target_identity,
            root_fd, target_fd, allow_empty=True,
        )
        candidates.sort(key=lambda item: (item[0].name.casefold(), item[0].name))

        entries: list[contract.DirectoryEntry] = []
        complete = True
        try:
            for entry, canonical_path, expected_identity, child_fd in candidates:
                registered_project = None
                try:
                    registered_project, _possible = self._registry.match_discovery(
                        node_id="local",
                        canonical_path=str(canonical_path),
                        repository_fingerprint=None,
                    )
                    registered_project, _ = _normalize_matches(
                        registered_project, ()
                    )
                except Exception:
                    complete = False
                try:
                    if (
                        _identity_fd(child_fd) != expected_identity
                        or _identity_at(target_fd, entry.name) != expected_identity
                    ):
                        raise OSError
                except (OSError, RuntimeError, ValueError):
                    raise contract.DiscoveryError("discovery_unavailable") from None
                entries.append(contract.DirectoryEntry(
                    name=entry.name,
                    path=entry.path,
                    kind=entry.kind,
                    vcs_hint=entry.vcs_hint,
                    registered_project=registered_project,
                ))
            self._require_unchanged(
                locator, root, target, root_identity, target_identity,
                root_fd, target_fd, allow_empty=True,
            )
        finally:
            for _entry, _path, _identity_value, child_fd in candidates:
                os.close(child_fd)
            os.close(target_fd)
            os.close(root_fd)
        sources = ("local_files",)
        if candidates and complete:
            sources += ("project_registry",)
        return contract.DirectoryListing(
            locator=locator,
            entries=tuple(entries),
            complete=complete,
            sources=sources,
            warnings=() if complete else ("project_registry_unavailable",),
        )

    def discover(
        self, locator: contract.ProjectLocator
    ) -> contract.DiscoveryResult:
        return _sanitized_call(lambda: self._discover(locator))

    def _discover(self, locator: contract.ProjectLocator) -> contract.DiscoveryResult:
        opened = self._open_locator(locator, allow_empty=True)
        root, target, root_identity, target_identity, root_fd, target_fd = opened
        try:
            vcs = self._discover_vcs(root.path, target, target_fd)
            self._require_unchanged(
                locator, root, target, root_identity, target_identity,
                root_fd, target_fd, allow_empty=True,
            )

            complete = True
            warnings: tuple[str, ...] = ()
            registry_ok = True
            try:
                exact, possible = self._registry.match_discovery(
                    node_id="local",
                    canonical_path=str(target),
                    repository_fingerprint=vcs.repository_fingerprint,
                )
                exact, possible = _normalize_matches(exact, possible)
                if vcs.kind != "git":
                    possible = ()
            except Exception:
                exact, possible = None, ()
                complete = False
                warnings = ("project_registry_unavailable",)
                registry_ok = False

            self._require_unchanged(
                locator, root, target, root_identity, target_identity,
                root_fd, target_fd, allow_empty=True,
            )
            canonical_digest = contract.sha256_text(
                "canonical-local-path-v1", str(target)
            )
            sources = ("local_files", "local_git")
            if registry_ok:
                sources += ("project_registry",)
            evidence = {
                "locator": locator.to_public_dict(),
                "canonical_path_digest": canonical_digest,
                "root_identity": root_identity.fingerprint_dict(),
                "target_identity": target_identity.fingerprint_dict(),
                "vcs": vcs.fingerprint_dict(),
                "exact_project_id": exact.project_id if exact else None,
                "possible_project_ids": [item.project_id for item in possible],
                "complete": complete,
                "warnings": list(warnings),
            }
            return contract.DiscoveryResult(
                locator=locator,
                display_path=_display_path(root.descriptor.display_name, locator.path),
                canonical_path_digest=canonical_digest,
                vcs=vcs,
                exact_match=exact,
                possible_projects=possible,
                discovery_fingerprint=contract.discovery_fingerprint(evidence),
                observed_at=datetime.now(UTC).isoformat(),
                complete=complete,
                sources=sources,
                warnings=warnings,
                _canonical_path=str(target),
            )
        finally:
            os.close(target_fd)
            os.close(root_fd)

    def _roots(self) -> dict[str, _Root]:
        try:
            candidates = self._root_reader.local_roots()
        except Exception:
            raise contract.DiscoveryError("root_forbidden") from None
        roots: dict[str, _Root] = {}
        seen: set[tuple[str, _Identity]] = set()
        for candidate in candidates:
            try:
                lexical = Path(candidate)
                if not lexical.is_absolute():
                    continue
                resolved = lexical.resolve(strict=True)
                identity = _identity(resolved)
            except (OSError, RuntimeError, ValueError):
                continue
            if not stat.S_ISDIR(identity.mode):
                continue
            key = (str(resolved), identity)
            if key in seen:
                continue
            seen.add(key)
            root_id = _root_id(resolved, identity)
            descriptor = contract.RootDescriptor(
                node_id="local",
                root_id=root_id,
                display_name=resolved.name or "root",
            )
            roots[root_id] = _Root(descriptor, resolved, identity)
        return dict(
            sorted(
                roots.items(),
                key=lambda item: (
                    item[1].descriptor.display_name.casefold(),
                    item[0],
                ),
            )
        )

    def _open_locator(
        self,
        locator: contract.ProjectLocator,
        *,
        allow_empty: bool,
    ) -> tuple[_Root, Path, _Identity, _Identity, int, int]:
        _validate_locator(locator, allow_empty=allow_empty)
        if locator.node_id != "local":
            raise contract.DiscoveryError("capability_unavailable")
        roots = self._roots()
        root = roots.get(locator.root_id)
        if root is None:
            raise contract.DiscoveryError("root_forbidden")
        root_fd = -1
        target_fd = -1
        try:
            root_fd = _open_absolute_directory(root.path)
            root_identity = _identity_fd(root_fd)
            if root_identity != root.identity:
                raise contract.DiscoveryError("root_forbidden")
            target_fd = os.dup(root_fd)
            for part in locator.path.split("/") if locator.path else ():
                next_fd = _open_component(target_fd, part)
                os.close(target_fd)
                target_fd = next_fd
            target_identity = _identity_fd(target_fd)
            target = root.path if not locator.path else root.path / locator.path
        except contract.DiscoveryError:
            if target_fd >= 0:
                os.close(target_fd)
            if root_fd >= 0:
                os.close(root_fd)
            raise
        except FileNotFoundError:
            if target_fd >= 0:
                os.close(target_fd)
            if root_fd >= 0:
                os.close(root_fd)
            raise contract.DiscoveryError("invalid_locator") from None
        except OSError:
            if target_fd >= 0:
                os.close(target_fd)
            if root_fd >= 0:
                os.close(root_fd)
            raise contract.DiscoveryError("discovery_unavailable") from None
        if not stat.S_ISDIR(target_identity.mode):
            os.close(target_fd)
            os.close(root_fd)
            raise contract.DiscoveryError("invalid_locator")
        return root, target, root_identity, target_identity, root_fd, target_fd

    def _require_unchanged(
        self,
        locator: contract.ProjectLocator,
        expected_root: _Root,
        expected_target: Path,
        expected_root_identity: _Identity,
        expected_target_identity: _Identity,
        root_fd: int,
        target_fd: int,
        *,
        allow_empty: bool,
    ) -> None:
        if (
            _identity_fd(root_fd) != expected_root_identity
            or _identity_fd(target_fd) != expected_target_identity
        ):
            raise contract.DiscoveryError("root_forbidden")
        opened = self._open_locator(locator, allow_empty=allow_empty)
        root, target, root_identity, target_identity, check_root_fd, check_target_fd = opened
        os.close(check_target_fd)
        os.close(check_root_fd)
        if (
            root.path != expected_root.path
            or target != expected_target
            or root_identity != expected_root_identity
            or target_identity != expected_target_identity
        ):
            raise contract.DiscoveryError("root_forbidden")

    def _discover_vcs(
        self, allowed_root: Path, target: Path, target_fd: int,
    ) -> contract.VcsObservation:
        first = self._observe_vcs(allowed_root, target, target_fd)
        if first.kind == "none":
            return first
        second = self._observe_vcs(allowed_root, target, target_fd)
        if first != second:
            raise contract.DiscoveryError("discovery_unavailable")
        return second

    def _observe_vcs(
        self, allowed_root: Path, target: Path, target_fd: int,
    ) -> contract.VcsObservation:
        top = self._git(target_fd, "rev-parse", "--show-toplevel", allow=(0, 128))
        if top.returncode == 128 and _is_not_git(top.stderr):
            return contract.VcsObservation(kind="none")
        if top.returncode != 0:
            raise contract.DiscoveryError("discovery_unavailable")
        top_text = _single_line(top.stdout)
        try:
            git_root = Path(top_text).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise contract.DiscoveryError("discovery_unavailable") from None
        if git_root != target:
            raise contract.DiscoveryError("invalid_locator")
        common_result = self._git(
            target_fd, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        try:
            common_dir = Path(_single_line(common_result.stdout)).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise contract.DiscoveryError("discovery_unavailable") from None
        if not _is_within(common_dir, allowed_root):
            raise contract.DiscoveryError("root_forbidden")

        head_result = self._git(target_fd, "rev-parse", "--verify", "HEAD", allow=(0, 128))
        branch_result = self._git(
            target_fd, "symbolic-ref", "--quiet", "--short", "HEAD", allow=(0, 1)
        )
        branch = _single_line(branch_result.stdout) if branch_result.returncode == 0 else None
        if head_result.returncode == 0:
            head = _single_line(head_result.stdout).lower()
            if not _HEX_SHA_RE.fullmatch(head):
                raise contract.DiscoveryError("discovery_unavailable")
            unborn = False
            detached = branch is None
        elif branch is not None:
            head = None
            unborn = True
            detached = False
        else:
            raise contract.DiscoveryError("discovery_unavailable")

        status = self._git(
            target_fd,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ).stdout
        status_digest = contract.sha256_text("git-status-v1", status)

        remote_result = self._git(
            target_fd, "config", "--get", "remote.origin.url", allow=(0, 1)
        )
        remote_fingerprint = None
        if remote_result.returncode == 0:
            remote = _normalize_remote(_single_line(remote_result.stdout))
            remote_fingerprint = contract.sha256_text("git-remote-v1", remote)

        refs_result = self._git(
            target_fd,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
            "refs/tags",
        )
        ref_rows: list[tuple[str, str]] = []
        for row in filter(None, refs_result.stdout.splitlines()):
            try:
                ref_name, object_name = row.rsplit(" ", 1)
            except ValueError:
                raise contract.DiscoveryError("discovery_unavailable") from None
            if _has_control(ref_name) or not _HEX_SHA_RE.fullmatch(object_name):
                raise contract.DiscoveryError("discovery_unavailable")
            ref_rows.append((ref_name, object_name.lower()))
        ref_rows.sort()
        if len(ref_rows) > MAX_REFS:
            raise contract.DiscoveryError("discovery_unavailable")
        refs_digest = contract.sha256_value({"refs": ref_rows})

        roots_result = self._git(
            target_fd, "rev-list", "--max-parents=0", "--all", allow=(0,)
        )
        history_roots = tuple(sorted(filter(None, roots_result.stdout.splitlines())))
        if (
            len(history_roots) > MAX_HISTORY_ROOTS
            or any(not _HEX_SHA_RE.fullmatch(item) for item in history_roots)
        ):
            raise contract.DiscoveryError("discovery_unavailable")
        repository_fingerprint = remote_fingerprint
        if repository_fingerprint is None and history_roots:
            repository_fingerprint = contract.sha256_value(
                {"kind": "git-root-commits-v1", "commits": history_roots}
            )

        upstream_result = self._git(
            target_fd,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
            allow=(0, 128),
        )
        upstream = None
        ahead = None
        behind = None
        if upstream_result.returncode == 0:
            upstream = _single_line(upstream_result.stdout)
            counts = self._git(
                target_fd,
                "rev-list",
                "--left-right",
                "--count",
                "HEAD...@{upstream}",
            )
            parts = counts.stdout.strip().split()
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise contract.DiscoveryError("discovery_unavailable")
            ahead, behind = int(parts[0]), int(parts[1])
        elif upstream_result.returncode != 128:
            raise contract.DiscoveryError("discovery_unavailable")

        return contract.VcsObservation(
            kind="git",
            git_root_digest=contract.sha256_text("git-root-v1", str(git_root)),
            remote_fingerprint=remote_fingerprint,
            repository_fingerprint=repository_fingerprint,
            head=head,
            branch_present=branch is not None,
            detached=detached,
            unborn=unborn,
            dirty=bool(status),
            status_digest=status_digest,
            refs_count=len(ref_rows),
            refs_digest=refs_digest,
            upstream_present=upstream is not None,
            ahead=ahead,
            behind=behind,
        )

    def _git(
        self,
        target_fd: int,
        *arguments: str,
        allow: tuple[int, ...] = (0,),
    ) -> CommandResult:
        environment = {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_ASKPASS": os.devnull,
            "SSH_ASKPASS": os.devnull,
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
            "LANG": "C",
        }
        result = self._runner.run(
            (
                _trusted_git_executable(),
                "--no-optional-locks",
                "-C",
                f"/proc/self/fd/{target_fd}",
                *arguments,
            ),
            environment=environment,
            timeout=GIT_TIMEOUT_SECONDS,
            max_output=MAX_GIT_OUTPUT,
        )
        if result.returncode not in allow:
            raise contract.DiscoveryError("discovery_unavailable")
        return result


def _validate_locator(locator: contract.ProjectLocator, *, allow_empty: bool) -> None:
    if not isinstance(locator, contract.ProjectLocator):
        raise contract.DiscoveryError("invalid_locator")
    if not isinstance(locator.node_id, str) or not locator.node_id:
        raise contract.DiscoveryError("node_not_found")
    if not isinstance(locator.root_id, str) or not _ROOT_ID_RE.fullmatch(locator.root_id):
        if locator.node_id != "local":
            return
        raise contract.DiscoveryError("root_forbidden")
    path = locator.path
    if not isinstance(path, str) or len(path) > MAX_PATH_LENGTH or _has_control(path):
        raise contract.DiscoveryError("invalid_locator")
    if path == "":
        if allow_empty:
            return
        raise contract.DiscoveryError("invalid_locator")
    if (
        path.startswith(("/", "~"))
        or path.endswith("/")
        or "//" in path
        or "\\" in path
    ):
        raise contract.DiscoveryError("invalid_locator")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise contract.DiscoveryError("invalid_locator")
    if PurePosixPath(path).is_absolute():
        raise contract.DiscoveryError("invalid_locator")


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _identity(path: Path) -> _Identity:
    info = path.stat()
    return _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _identity_fd(fd: int) -> _Identity:
    info = os.fstat(fd)
    return _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _identity_at(parent_fd: int, name: str) -> _Identity:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _open_component(parent_fd: int, name: str) -> int:
    """Open one no-follow directory component relative to a descriptor.

    ``O_NOFOLLOW | O_DIRECTORY`` rejects a symlink or non-directory component
    with ``ENOTDIR`` (a symlink is not itself a directory). A symlink component
    is a capability escape and maps to ``root_forbidden``; a non-directory
    regular entry maps to ``invalid_locator``. Classification uses a
    descriptor-relative no-follow ``lstat`` so it cannot follow an escape, and a
    concurrent swap can only downgrade the code, never read outside the root.
    """
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError:
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except OSError:
            raise contract.DiscoveryError("discovery_unavailable") from None
        if stat.S_ISLNK(info.st_mode):
            raise contract.DiscoveryError("root_forbidden") from None
        raise contract.DiscoveryError("invalid_locator") from None


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute():
        raise OSError
    current = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            next_fd = _open_component(current, part)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _root_id(path: Path, identity: _Identity) -> str:
    digest = contract.sha256_value(
        {
            "kind": "local-root-v1",
            "path": str(path),
            "identity": identity.fingerprint_dict(),
        }
    )
    return "root_" + digest.removeprefix("sha256:")[:24]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _vcs_hint_at(parent_fd: int, name: str) -> str:
    child_fd = -1
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        marker = os.stat(".git", dir_fd=child_fd, follow_symlinks=False)
        if stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode):
            return "git"
    except OSError:
        pass
    finally:
        if child_fd >= 0:
            os.close(child_fd)
    return "unknown"


def _trusted_git_executable() -> str:
    for candidate in (Path("/usr/bin/git"), Path("/usr/local/bin/git")):
        try:
            info = candidate.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(info.st_mode)
            and not stat.S_IMODE(info.st_mode) & 0o022
            and info.st_uid in {0, os.getuid()}
        ):
            return str(candidate)
    raise contract.DiscoveryError("discovery_unavailable")


def _sanitized_call(callable_):
    code: str | None = None
    try:
        return callable_()
    except contract.DiscoveryError as exc:
        code = exc.code
    except Exception:
        code = "discovery_unavailable"
    raise contract.DiscoveryError(code) from None


def _display_path(root_name: str, relative: str) -> str:
    return root_name if not relative else f"{root_name}/{relative}"


def _single_line(value: str) -> str:
    stripped = value.rstrip("\r\n")
    if not stripped or "\n" in stripped or "\r" in stripped or _has_control(stripped):
        raise contract.DiscoveryError("discovery_unavailable")
    return stripped


def _is_not_git(stderr: str) -> bool:
    return "not a git repository" in stderr.lower()


def _normalize_remote(value: str) -> str:
    if _has_control(value):
        raise contract.DiscoveryError("discovery_unavailable")
    candidate = value.strip()
    if not candidate:
        raise contract.DiscoveryError("discovery_unavailable")
    scp_match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", candidate)
    if scp_match and "://" not in candidate:
        host = scp_match.group(1).lower()
        path = scp_match.group(2)
        normalized = f"{host}/{path}"
    else:
        try:
            parsed = urlsplit(candidate)
            parsed_host = parsed.hostname
            parsed_port = parsed.port
        except ValueError:
            raise contract.DiscoveryError("discovery_unavailable") from None
        if parsed.scheme and parsed_host:
            host = parsed_host.lower()
            port = f":{parsed_port}" if parsed_port else ""
            normalized = f"{host}{port}/{parsed.path.lstrip('/')}"
        else:
            normalized = candidate
    normalized = normalized.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if not normalized:
        raise contract.DiscoveryError("discovery_unavailable")
    return normalized


def _normalize_matches(
    exact: contract.RegistryMatch | None,
    possible: Sequence[contract.RegistryMatch],
) -> tuple[contract.RegistryMatch | None, tuple[contract.RegistryMatch, ...]]:
    if exact is not None and not isinstance(exact, contract.RegistryMatch):
        raise contract.DiscoveryError("discovery_unavailable")
    if exact is not None:
        _validate_match(exact)
    if len(possible) > MAX_POSSIBLE_PROJECTS:
        raise contract.DiscoveryError("discovery_unavailable")
    deduplicated: dict[str, contract.RegistryMatch] = {}
    for match in possible:
        if not isinstance(match, contract.RegistryMatch):
            raise contract.DiscoveryError("discovery_unavailable")
        _validate_match(match)
        if exact is not None and match.project_id == exact.project_id:
            continue
        deduplicated.setdefault(match.project_id, match)
    return exact, tuple(deduplicated[key] for key in sorted(deduplicated))


def _validate_match(match: contract.RegistryMatch) -> None:
    values = (
        (match.project_id, 64),
        (match.slug, 64),
        (match.display_name, 256),
    )
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or _has_control(value)
        for value, maximum in values
    ):
        raise contract.DiscoveryError("discovery_unavailable")


DiscoveryError = contract.DiscoveryError
ProjectLocator = contract.ProjectLocator
RegistryMatch = contract.RegistryMatch
