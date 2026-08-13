from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_cockpit.project_discovery import (
    DiscoveryError,
    ProjectLocator,
    RegistryMatch,
)
from agent_cockpit.project_discovery_service import LocalProjectDiscoveryService
from agent_cockpit import project_discovery_service as discovery_service


@dataclass
class FakeRootReader:
    paths: tuple[Path, ...]
    calls: int = 0

    def local_roots(self) -> tuple[Path, ...]:
        self.calls += 1
        return self.paths


@dataclass
class FakeRegistryMatchReader:
    exact: RegistryMatch | None = None
    possible: tuple[RegistryMatch, ...] = ()
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def match_discovery(
        self,
        *,
        node_id: str,
        canonical_path: str,
        repository_fingerprint: str | None,
    ) -> tuple[RegistryMatch | None, tuple[RegistryMatch, ...]]:
        self.calls.append((node_id, canonical_path, repository_fingerprint))
        return self.exact, self.possible


def _service(
    root: Path,
    registry: FakeRegistryMatchReader | None = None,
) -> tuple[LocalProjectDiscoveryService, str, FakeRegistryMatchReader]:
    registry = registry or FakeRegistryMatchReader()
    service = LocalProjectDiscoveryService(
        root_reader=FakeRootReader((root,)),
        registry_match_reader=registry,
    )
    roots = service.list_roots()
    assert len(roots) == 1
    assert roots[0].display_name == root.name
    assert str(root) not in roots[0].to_public_dict().values()
    return service, roots[0].root_id, registry


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Discovery Test",
            "GIT_AUTHOR_EMAIL": "discovery@example.invalid",
            "GIT_COMMITTER_NAME": "Discovery Test",
            "GIT_COMMITTER_EMAIL": "discovery@example.invalid",
        },
    )
    return completed.stdout.strip()


def _init_git_repo(repo: Path) -> str:
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _tree_digest(root: Path) -> str:
    rows: list[bytes] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root)).encode("utf-8")
        info = path.lstat()
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8")
            kind = b"link"
        elif path.is_file():
            payload = path.read_bytes()
            kind = b"file"
        else:
            payload = b""
            kind = b"dir"
        rows.extend((relative, kind, str(info.st_mode).encode("ascii"), payload))
    return hashlib.sha256(b"\0".join(rows)).hexdigest()


def _code(error: pytest.ExceptionInfo[DiscoveryError]) -> str:
    return error.value.code


def test_allowlisted_root_lists_only_direct_real_directories(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    (root / "zeta").mkdir()
    (root / "Alpha").mkdir()
    (root / "file.txt").write_text("not a directory", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    service, root_id, _ = _service(root)
    listing = service.list_directories(ProjectLocator("local", root_id, ""))

    assert [entry.name for entry in listing.entries] == ["Alpha", "zeta"]
    assert all(entry.kind == "directory" for entry in listing.entries)
    assert all(not Path(entry.path).is_absolute() for entry in listing.entries)
    assert all(entry.registered_project is None for entry in listing.entries)
    assert listing.partial is False
    assert listing.sources == ("local_files", "project_registry")
    assert listing.warnings == ()


def test_directory_listing_includes_exact_registered_project_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    child = root / "registered"
    child.mkdir(parents=True)
    exact = RegistryMatch("prj_exact", "registered", "Registered Project")
    registry = FakeRegistryMatchReader(exact=exact)
    service, root_id, _ = _service(root, registry)

    listing = service.list_directories(ProjectLocator("local", root_id, ""))

    assert listing.complete is True
    assert listing.entries[0].registered_project == exact
    assert listing.to_public_dict()["entries"][0]["registered_project"] == (
        exact.to_public_dict()
    )
    assert registry.calls == [("local", str(child.resolve()), None)]


def test_directory_listing_registry_failure_is_explicit_partial_source(
    tmp_path: Path,
) -> None:
    class FailingRegistry:
        def match_discovery(self, **_kwargs):
            raise RuntimeError("private registry detail")

    root = tmp_path / "projects"
    (root / "unknown").mkdir(parents=True)
    service = LocalProjectDiscoveryService(
        FakeRootReader((root,)), FailingRegistry()
    )
    root_id = service.list_roots()[0].root_id

    listing = service.list_directories(ProjectLocator("local", root_id, ""))
    public = listing.to_public_dict()

    assert listing.entries[0].registered_project is None
    assert listing.complete is False
    assert listing.partial is True
    assert listing.sources == ("local_files",)
    assert listing.warnings == ("project_registry_unavailable",)
    assert public["partial"] is True
    assert public["sources"] == ["local_files"]
    assert "private registry detail" not in repr(public)


def test_root_id_is_stable_but_does_not_disclose_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()

    first, _, _ = _service(root)
    second, _, _ = _service(root)

    assert first.list_roots()[0].root_id == second.list_roots()[0].root_id
    assert str(root) not in first.list_roots()[0].root_id


@pytest.mark.parametrize(
    "path",
    ["/absolute", "../escape", "a/../escape", "./repo", "a//b", "repo/", "a\\b", "bad\x00path"],
)
def test_locator_rejects_absolute_parent_and_noncanonical_paths(
    tmp_path: Path, path: str,
) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    service, root_id, registry = _service(root)

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", root_id, path))

    assert _code(error) == "invalid_locator"
    assert registry.calls == []


def test_unknown_root_is_forbidden_without_registry_access(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    service, _, registry = _service(root)

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", "root_unknown", "repo"))

    assert _code(error) == "root_forbidden"
    assert registry.calls == []


def test_non_local_node_fails_closed_without_local_fallback(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    root_reader = FakeRootReader((root,))
    registry = FakeRegistryMatchReader()
    service = LocalProjectDiscoveryService(root_reader, registry)

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("ssh-node", "root_unused", "repo"))

    assert _code(error) == "capability_unavailable"
    assert root_reader.calls == 0
    assert registry.calls == []


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    service, root_id, registry = _service(root)

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", root_id, "escape"))

    assert _code(error) == "root_forbidden"
    assert registry.calls == []


def test_non_git_directory_is_a_complete_stable_read_only_result(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    repo = root / "plain"
    repo.mkdir(parents=True)
    (repo / "notes.txt").write_text("plain directory\n", encoding="utf-8")
    service, root_id, registry = _service(root)
    locator = ProjectLocator("local", root_id, "plain")
    before = _tree_digest(root)

    first = service.discover(locator)
    second = service.discover(locator)

    assert first.vcs.kind == "none"
    assert first.vcs.head is None
    assert first.complete is True
    assert first.discovery_fingerprint == second.discovery_fingerprint
    assert first.observed_at != ""
    assert first.to_public_dict()["canonical_path_digest"].startswith("sha256:")
    assert str(repo) not in repr(first.to_public_dict())
    assert _tree_digest(root) == before
    assert len(registry.calls) == 2
    assert registry.calls[0][2] is None


def test_non_git_directory_ignores_repository_suggestions(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    repo = root / "plain"
    repo.mkdir(parents=True)
    exact = RegistryMatch("prj_exact", "exact", "Exact")
    suggestion = RegistryMatch("prj_suggested", "suggested", "Suggested")
    registry = FakeRegistryMatchReader(exact=exact, possible=(suggestion,))
    service, root_id, _ = _service(root, registry)

    result = service.discover(ProjectLocator("local", root_id, "plain"))

    assert result.vcs.kind == "none"
    assert result.exact_match == exact
    assert result.possible_projects == ()


def test_git_discovery_observes_head_branch_dirty_and_hides_remote(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "git-repo"
    head = _init_git_repo(repo)
    _git(repo, "remote", "add", "origin", "https://user:secret@example.invalid/acme/repo.git")
    service, root_id, _ = _service(root)
    locator = ProjectLocator("local", root_id, "git-repo")

    clean = service.discover(locator)
    clean_repeated = service.discover(locator)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    dirty = service.discover(locator)

    assert clean.vcs.kind == "git"
    assert clean.vcs.head == head
    assert clean.vcs.branch_present is True
    assert clean.vcs.dirty is False
    assert clean.vcs.remote_fingerprint.startswith("sha256:")
    assert clean.discovery_fingerprint == clean_repeated.discovery_fingerprint
    assert dirty.vcs.dirty is True
    assert clean.vcs.status_digest != dirty.vcs.status_digest
    assert clean.discovery_fingerprint != dirty.discovery_fingerprint
    assert "secret" not in repr(clean.to_public_dict())
    assert "example.invalid" not in repr(clean.to_public_dict())


def test_detached_git_head_and_missing_remote_are_valid(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "detached"
    head = _init_git_repo(repo)
    _git(repo, "checkout", "--detach", "-q", head)
    service, root_id, _ = _service(root)

    result = service.discover(ProjectLocator("local", root_id, "detached"))

    assert result.vcs.kind == "git"
    assert result.vcs.head == head
    assert result.vcs.branch_present is False
    assert result.vcs.detached is True
    assert result.vcs.remote_fingerprint is None


def test_registry_match_reader_is_advisory_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    _init_git_repo(repo)
    exact = RegistryMatch("prj_exact", "exact-project", "Exact Project")
    possible = RegistryMatch("prj_possible", "possible-project", "Possible Project")
    registry = FakeRegistryMatchReader(exact=exact, possible=(possible, possible))
    service, root_id, _ = _service(root, registry)

    result = service.discover(ProjectLocator("local", root_id, "repo"))

    assert result.exact_match == exact
    assert result.possible_projects == (possible,)
    assert len(registry.calls) == 1
    assert registry.calls[0][0] == "local"
    assert registry.calls[0][1] == str(repo.resolve())
    assert registry.calls[0][2] == result.vcs.repository_fingerprint


def test_match_evidence_changes_discovery_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    (root / "plain").mkdir()
    registry = FakeRegistryMatchReader()
    service, root_id, _ = _service(root, registry)
    locator = ProjectLocator("local", root_id, "plain")

    unmatched = service.discover(locator)
    registry.exact = RegistryMatch("prj_exact", "exact-project", "Exact Project")
    matched = service.discover(locator)

    assert unmatched.discovery_fingerprint != matched.discovery_fingerprint


def test_git_probe_and_registry_match_do_not_mutate_repository(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    head = _init_git_repo(repo)
    registry = FakeRegistryMatchReader()
    service, root_id, _ = _service(root, registry)
    before_tree = _tree_digest(repo)
    before_status = _git(repo, "status", "--porcelain=v1", "--untracked-files=normal")
    before_refs = _git(repo, "show-ref")

    service.discover(ProjectLocator("local", root_id, "repo"))

    assert _tree_digest(repo) == before_tree
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=normal") == before_status
    assert _git(repo, "show-ref") == before_refs
    assert len(registry.calls) == 1


def test_default_registry_reader_is_degraded_not_complete_empty_match(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    repo = root / "plain"
    repo.mkdir(parents=True)
    service = LocalProjectDiscoveryService(root_reader=FakeRootReader((root,)))
    root_id = service.list_roots()[0].root_id

    result = service.discover(ProjectLocator("local", root_id, "plain"))

    assert result.complete is False
    assert result.warnings == ("project_registry_unavailable",)
    assert result.exact_match is None
    assert result.possible_projects == ()


def test_git_runner_receives_read_only_bounded_environment(tmp_path: Path) -> None:
    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[str, ...], dict[str, str], float, int]] = []

        def run(self, argv, *, environment, timeout, max_output):
            self.calls.append((tuple(argv), dict(environment), timeout, max_output))
            return type("Result", (), {
                "returncode": 128,
                "stdout": "",
                "stderr": "fatal: not a git repository\n",
            })()

    root = tmp_path / "projects"
    repo = root / "plain"
    repo.mkdir(parents=True)
    runner = RecordingRunner()
    service = LocalProjectDiscoveryService(
        FakeRootReader((root,)), FakeRegistryMatchReader(), runner
    )
    root_id = service.list_roots()[0].root_id

    result = service.discover(ProjectLocator("local", root_id, "plain"))

    assert result.vcs.kind == "none"
    assert len(runner.calls) == 1
    argv, environment, timeout, max_output = runner.calls[0]
    assert Path(argv[0]).is_absolute()
    assert argv[1] == "--no-optional-locks"
    assert environment.get("PATH") is None
    assert environment.get("LD_PRELOAD") is None
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_KEY_0"] == "core.fsmonitor"
    assert environment["GIT_CONFIG_VALUE_0"] == "false"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_ATTR_NOSYSTEM"] == "1"
    assert timeout > 0
    assert max_output > 0


def test_target_replaced_during_probe_fails_closed(tmp_path: Path) -> None:
    class ReplacingRunner:
        def __init__(self, target: Path) -> None:
            self.target = target
            self.called = False

        def run(self, argv, *, environment, timeout, max_output):
            if not self.called:
                self.called = True
                self.target.rename(self.target.with_name("moved"))
                self.target.mkdir()
            return type("Result", (), {
                "returncode": 128,
                "stdout": "",
                "stderr": "fatal: not a git repository\n",
            })()

    root = tmp_path / "projects"
    target = root / "repo"
    target.mkdir(parents=True)
    service = LocalProjectDiscoveryService(
        FakeRootReader((root,)), FakeRegistryMatchReader(), ReplacingRunner(target)
    )
    root_id = service.list_roots()[0].root_id

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", root_id, "repo"))

    assert _code(error) == "root_forbidden"


def test_unexpected_git_failure_is_not_misclassified_as_non_git(tmp_path: Path) -> None:
    class FailingRunner:
        def run(self, argv, *, environment, timeout, max_output):
            return type("Result", (), {
                "returncode": 128,
                "stdout": "",
                "stderr": "fatal: unsafe repository ownership\n",
            })()

    root = tmp_path / "projects"
    target = root / "repo"
    target.mkdir(parents=True)
    service = LocalProjectDiscoveryService(
        FakeRootReader((root,)), FakeRegistryMatchReader(), FailingRunner()
    )
    root_id = service.list_roots()[0].root_id

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", root_id, "repo"))

    assert _code(error) == "discovery_unavailable"


def test_other_ref_target_change_changes_discovery_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    first_head = _init_git_repo(repo)
    _git(repo, "branch", "side", first_head)
    service, root_id, _ = _service(root)
    locator = ProjectLocator("local", root_id, "repo")
    before = service.discover(locator)

    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    advanced = _git(repo, "commit-tree", tree, "-p", first_head, "-m", "advance side")
    _git(repo, "update-ref", "refs/heads/side", advanced, first_head)
    after = service.discover(locator)

    assert before.vcs.head == after.vcs.head == first_head
    assert before.vcs.refs_count == after.vcs.refs_count
    assert before.vcs.refs_digest != after.vcs.refs_digest
    assert before.discovery_fingerprint != after.discovery_fingerprint


def test_public_vcs_evidence_does_not_disclose_ref_names(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    head = _init_git_repo(repo)
    secret_ref = "refs/tags/customer-secret-release"
    _git(repo, "update-ref", secret_ref, head)
    service, root_id, _ = _service(root)

    result = service.discover(ProjectLocator("local", root_id, "repo"))
    public = result.to_public_dict()

    assert result.vcs.refs_count == 2
    assert "refs" not in public["vcs"]
    assert "customer-secret-release" not in repr(public)


def test_git_change_during_probe_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    _init_git_repo(repo)

    class MutatingRunner:
        def __init__(self) -> None:
            self.delegate = discovery_service.SubprocessCommandRunner()
            self.mutated = False

        def run(self, argv, *, environment, timeout, max_output):
            result = self.delegate.run(
                argv,
                environment=environment,
                timeout=timeout,
                max_output=max_output,
            )
            if not self.mutated and tuple(argv[-3:]) == (
                "rev-parse",
                "--verify",
                "HEAD",
            ):
                self.mutated = True
                (repo / "README.md").write_text("changed during probe\n", encoding="utf-8")
                _git(repo, "add", "README.md")
                _git(repo, "commit", "-qm", "change during probe")
            return result

    service = LocalProjectDiscoveryService(
        FakeRootReader((root,)), FakeRegistryMatchReader(), MutatingRunner()
    )
    root_id = service.list_roots()[0].root_id

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", root_id, "repo"))

    assert _code(error) == "discovery_unavailable"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_subprocess_runner_kills_and_reaps_on_output_limit(
    monkeypatch: pytest.MonkeyPatch, stream: str,
) -> None:
    created: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(discovery_service.subprocess, "Popen", recording_popen)
    script = (
        "import sys; "
        f"sys.{stream}.buffer.write(b'x' * 4096); sys.{stream}.flush(); "
        "import time; time.sleep(10)"
    )
    runner = discovery_service.SubprocessCommandRunner()

    with pytest.raises(DiscoveryError) as error:
        runner.run(
            (sys.executable, "-c", script),
            environment=os.environ,
            timeout=2,
            max_output=64,
        )

    assert _code(error) == "discovery_unavailable"
    assert len(created) == 1
    assert created[0].poll() is not None


def test_subprocess_runner_kills_and_reaps_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(discovery_service.subprocess, "Popen", recording_popen)
    runner = discovery_service.SubprocessCommandRunner()
    started = time.monotonic()

    with pytest.raises(DiscoveryError) as error:
        runner.run(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            environment=os.environ,
            timeout=0.05,
            max_output=64,
        )

    assert time.monotonic() - started < 2
    assert _code(error) == "discovery_unavailable"
    assert len(created) == 1
    assert created[0].poll() is not None


def test_subprocess_runner_enforces_aggregate_output_limit() -> None:
    runner = discovery_service.SubprocessCommandRunner()
    script = "import os; os.write(1,b'o'*64); os.write(2,b'e'*64)"

    with pytest.raises(DiscoveryError) as error:
        runner.run(
            (sys.executable, "-c", script),
            environment={},
            timeout=2,
            max_output=64,
        )

    assert _code(error) == "discovery_unavailable"


def test_external_global_fsmonitor_config_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    _init_git_repo(repo)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "malicious-fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n", encoding="utf-8")
    hook.chmod(0o700)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        f"[core]\n\tfsmonitor = {hook}\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    service, root_id, _ = _service(root)

    result = service.discover(ProjectLocator("local", root_id, "repo"))

    assert result.vcs.kind == "git"
    assert not marker.exists()


def test_malformed_remote_port_is_stable_discovery_error(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    _init_git_repo(repo)
    _git(repo, "remote", "add", "origin", "ssh://example.invalid:notaport/repo.git")
    service, root_id, _ = _service(root)

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", root_id, "repo"))

    assert _code(error) == "discovery_unavailable"


def test_discovery_error_traceback_suppresses_raw_path_cause(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    service, root_id, _ = _service(root)
    secret_component = "customer-secret-missing"

    with pytest.raises(DiscoveryError) as error:
        service.discover(ProjectLocator("local", root_id, secret_component))

    rendered = "".join(
        traceback.format_exception(
            type(error.value), error.value, error.value.__traceback__
        )
    )
    assert error.value.__cause__ is None
    assert str(root) not in rendered
    assert secret_component not in rendered


def test_refs_and_history_root_limits_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = root / "repo"
    head = _init_git_repo(repo)
    _git(repo, "branch", "second", head)
    service, root_id, _ = _service(root)
    locator = ProjectLocator("local", root_id, "repo")

    monkeypatch.setattr(discovery_service, "MAX_REFS", 1)
    with pytest.raises(DiscoveryError) as refs_error:
        service.discover(locator)
    assert _code(refs_error) == "discovery_unavailable"

    monkeypatch.setattr(discovery_service, "MAX_REFS", 4096)
    monkeypatch.setattr(discovery_service, "MAX_HISTORY_ROOTS", 0)
    with pytest.raises(DiscoveryError) as roots_error:
        service.discover(locator)
    assert _code(roots_error) == "discovery_unavailable"


@pytest.mark.parametrize(
    "exact,possible",
    [
        (object(), ()),
        (None, (object(),)),
        (RegistryMatch("bad\nproject", "slug", "Display"), ()),
    ],
)
def test_invalid_registry_match_values_degrade_instead_of_escaping(
    tmp_path: Path, exact, possible,
) -> None:
    root = tmp_path / "projects"
    repo = root / "plain"
    repo.mkdir(parents=True)
    registry = FakeRegistryMatchReader()
    registry.exact = exact
    registry.possible = possible
    service, root_id, _ = _service(root, registry)

    result = service.discover(ProjectLocator("local", root_id, "plain"))

    assert result.complete is False
    assert result.warnings == ("project_registry_unavailable",)
    assert result.exact_match is None
    assert result.possible_projects == ()
