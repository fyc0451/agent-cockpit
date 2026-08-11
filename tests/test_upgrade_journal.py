from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import upgrade_journal as journal


DIGEST = "d" * 64
REQUEST = "request-1"


def _create(tmp_path: Path) -> Path:
    root = tmp_path / "controller"
    value = journal.create_journal(
        root=root, request_id=REQUEST, target_digest=DIGEST
    )
    assert value["stage"] == "prepared"
    assert value["intent"] == journal.INTENT_STOP_SERVICES
    return root


def _advance(root: Path, intent: str, stage: str) -> dict[str, object]:
    current = journal.record_intent(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        intent=intent,
    )
    assert current["intent"] == intent
    return journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage=stage,
    )


def test_happy_path_records_each_intent_before_forward_transition(tmp_path: Path) -> None:
    root = _create(tmp_path)
    value = journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage="services_stopped",
    )
    assert value["intent"] is None
    value = _advance(root, journal.INTENT_SWITCH_CURRENT, "switched")
    value = _advance(root, journal.INTENT_START_SERVICES, "services_started")
    value = _advance(root, journal.INTENT_COMMIT, "committed")
    assert value["stage"] == "committed"
    assert value["revision"] == 7
    assert journal.load_journal(root=root) == value


def test_root_and_journal_have_exact_private_modes(tmp_path: Path) -> None:
    root = _create(tmp_path)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    path = root / journal.JOURNAL_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1


def test_schema_is_fixed_and_canonical(tmp_path: Path) -> None:
    root = _create(tmp_path)
    raw = (root / journal.JOURNAL_NAME).read_bytes()
    assert raw.endswith(b"\n")
    assert json.dumps(
        json.loads(raw), sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n" == raw
    assert set(json.loads(raw)) == {
        "schema_version",
        "engine",
        "request_id",
        "target_digest",
        "stage",
        "intent",
        "revision",
        "primary_error_code",
        "rollback_error_code",
    }


def test_load_is_pure_read_when_root_or_file_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    with pytest.raises(journal.UpgradeJournalError) as missing_root:
        journal.load_journal(root=root)
    assert missing_root.value.code == "journal_missing"
    assert not root.exists()

    root.mkdir(mode=0o700)
    before = root.stat().st_mtime_ns
    with pytest.raises(journal.UpgradeJournalError) as missing_file:
        journal.load_journal(root=root)
    assert missing_file.value.code == "journal_missing"
    assert list(root.iterdir()) == []
    assert root.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    ("request_id", "target_digest"),
    [
        ("", DIGEST),
        ("bad\x00request", DIGEST),
        ("x" * 201, DIGEST),
        (True, DIGEST),
        (REQUEST, "D" * 64),
        (REQUEST, True),
    ],
)
def test_identity_types_and_values_are_strict(
    tmp_path: Path, request_id: object, target_digest: object
) -> None:
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.create_journal(
            root=tmp_path / "controller",
            request_id=request_id,  # type: ignore[arg-type]
            target_digest=target_digest,  # type: ignore[arg-type]
        )
    assert exc.value.code == "journal_identity_invalid"


def test_mutations_require_exact_request_and_target_identity(tmp_path: Path) -> None:
    root = _create(tmp_path)
    for request_id, target_digest in (("other", DIGEST), (REQUEST, "e" * 64)):
        with pytest.raises(journal.UpgradeJournalError) as exc:
            journal.advance_journal(
                root=root,
                request_id=request_id,
                target_digest=target_digest,
                stage="services_stopped",
            )
        assert exc.value.code == "journal_identity_mismatch"
    assert journal.load_journal(root=root)["revision"] == 0


@pytest.mark.parametrize(
    "stage", ["switched", "services_started", "committed", "rolled_back", True]
)
def test_transitions_cannot_skip_or_change_direction(tmp_path: Path, stage: object) -> None:
    root = _create(tmp_path)
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.advance_journal(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            stage=stage,  # type: ignore[arg-type]
        )
    assert exc.value.code == "journal_transition_invalid"


def test_next_mutation_requires_a_durable_matching_intent(tmp_path: Path) -> None:
    root = _create(tmp_path)
    journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage="services_stopped",
    )
    with pytest.raises(journal.UpgradeJournalError) as no_intent:
        journal.advance_journal(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            stage="switched",
        )
    assert no_intent.value.code == "journal_transition_invalid"
    with pytest.raises(journal.UpgradeJournalError) as wrong_intent:
        journal.record_intent(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            intent=journal.INTENT_START_SERVICES,
        )
    assert wrong_intent.value.code == "journal_transition_invalid"


def test_rollback_preserves_sanitized_primary_and_rollback_codes(tmp_path: Path) -> None:
    root = _create(tmp_path)
    value = journal.record_intent(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        intent=journal.INTENT_ROLLBACK,
        primary_error_code="stop_failed",
    )
    assert value["primary_error_code"] == "stop_failed"
    value = journal.record_rollback_error(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        rollback_error_code="restart_old_failed",
    )
    assert value["rollback_error_code"] == "restart_old_failed"
    value = journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage="rolled_back",
    )
    assert value["stage"] == "rolled_back"
    assert value["primary_error_code"] == "stop_failed"
    assert value["rollback_error_code"] == "restart_old_failed"


@pytest.mark.parametrize(
    "error_code",
    ["", "UPPER", "has-dash", "path_/tmp/secret", "x" * 65, True],
)
def test_error_codes_cannot_contain_exception_text_paths_or_credentials(
    tmp_path: Path, error_code: object
) -> None:
    root = _create(tmp_path)
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.record_intent(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            intent=journal.INTENT_ROLLBACK,
            primary_error_code=error_code,  # type: ignore[arg-type]
        )
    assert exc.value.code == "journal_error_code_invalid"


def test_rollback_error_code_is_also_strict(tmp_path: Path) -> None:
    root = _create(tmp_path)
    journal.record_intent(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        intent=journal.INTENT_ROLLBACK,
        primary_error_code="switch_failed",
    )
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.record_rollback_error(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            rollback_error_code="exception: /secret/path",
        )
    assert exc.value.code == "journal_error_code_invalid"


def test_repeated_intent_and_terminal_calls_are_idempotent(tmp_path: Path) -> None:
    root = _create(tmp_path)
    initial = journal.load_journal(root=root)
    repeated = journal.record_intent(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        intent=journal.INTENT_STOP_SERVICES,
    )
    assert repeated == initial
    journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage="services_stopped",
    )
    _advance(root, journal.INTENT_SWITCH_CURRENT, "switched")
    _advance(root, journal.INTENT_START_SERVICES, "services_started")
    terminal = _advance(root, journal.INTENT_COMMIT, "committed")
    repeated_terminal = journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage="committed",
    )
    assert repeated_terminal == terminal


def test_terminal_stage_cannot_be_reopened(tmp_path: Path) -> None:
    root = _create(tmp_path)
    value = journal.record_intent(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        intent=journal.INTENT_ROLLBACK,
        primary_error_code="switch_failed",
    )
    assert value["intent"] == journal.INTENT_ROLLBACK
    journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage="rolled_back",
    )
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.record_intent(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            intent=journal.INTENT_STOP_SERVICES,
        )
    assert exc.value.code == "journal_transition_invalid"


@pytest.mark.parametrize("corruption", ["truncated", "duplicate", "extra"])
def test_load_rejects_corrupt_or_non_allowlisted_json(
    tmp_path: Path, corruption: str
) -> None:
    root = _create(tmp_path)
    path = root / journal.JOURNAL_NAME
    if corruption == "truncated":
        raw = path.read_bytes()[:-4]
    elif corruption == "duplicate":
        raw = b'{"schema_version":1,"schema_version":1}\n'
    else:
        value = json.loads(path.read_bytes())
        value["unexpected"] = True
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.load_journal(root=root)
    assert exc.value.code == "journal_invalid"


@pytest.mark.parametrize("target", ["root_symlink", "file_symlink", "hardlink"])
def test_load_rejects_symlink_and_hardlink_targets(tmp_path: Path, target: str) -> None:
    root = _create(tmp_path)
    path = root / journal.JOURNAL_NAME
    if target == "root_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif target == "file_symlink":
        real = root / "real.json"
        path.rename(real)
        path.symlink_to(real)
    else:
        os.link(path, root / "second-link")
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.load_journal(root=root)
    assert exc.value.code == "journal_unsafe"


@pytest.mark.parametrize(("target", "mode"), [("root", 0o755), ("file", 0o644)])
def test_load_rejects_non_private_modes(tmp_path: Path, target: str, mode: int) -> None:
    root = _create(tmp_path)
    (root if target == "root" else root / journal.JOURNAL_NAME).chmod(mode)
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.load_journal(root=root)
    assert exc.value.code == "journal_unsafe"


def test_load_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _create(tmp_path)
    real_uid = os.getuid()
    monkeypatch.setattr(journal.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.load_journal(root=root)
    assert exc.value.code == "journal_unsafe"


def test_atomic_write_fsyncs_file_before_replace_and_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _create(tmp_path)
    events: list[str] = []
    real_fsync = journal.os.fsync
    real_replace = journal.os.replace

    def tracked_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        events.append("dir_fsync" if stat.S_ISDIR(mode) else "file_fsync")
        real_fsync(fd)

    def tracked_replace(*args: object, **kwargs: object) -> None:
        events.append("replace")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(journal.os, "fsync", tracked_fsync)
    monkeypatch.setattr(journal.os, "replace", tracked_replace)
    journal.advance_journal(
        root=root,
        request_id=REQUEST,
        target_digest=DIGEST,
        stage="services_stopped",
    )
    assert events == ["file_fsync", "replace", "dir_fsync"]


def test_failure_before_replace_preserves_previous_durable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _create(tmp_path)
    before = (root / journal.JOURNAL_NAME).read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("sensitive path must not escape")

    monkeypatch.setattr(journal.os, "replace", fail_replace)
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.advance_journal(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            stage="services_stopped",
        )
    assert exc.value.code == "journal_io_error"
    assert str(exc.value) == "journal_io_error"
    assert (root / journal.JOURNAL_NAME).read_bytes() == before
    assert list(root.glob(f".{journal.JOURNAL_NAME}.*.tmp")) == []


def test_file_fsync_failure_preserves_previous_durable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _create(tmp_path)
    before = (root / journal.JOURNAL_NAME).read_bytes()
    real_fsync = journal.os.fsync

    def fail_file_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("raw storage failure")
        real_fsync(fd)

    monkeypatch.setattr(journal.os, "fsync", fail_file_fsync)
    with pytest.raises(journal.UpgradeJournalError) as exc:
        journal.advance_journal(
            root=root,
            request_id=REQUEST,
            target_digest=DIGEST,
            stage="services_stopped",
        )
    assert exc.value.code == "journal_io_error"
    assert (root / journal.JOURNAL_NAME).read_bytes() == before
    assert list(root.glob(f".{journal.JOURNAL_NAME}.*.tmp")) == []
