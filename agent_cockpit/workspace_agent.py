"""Workspace-scoped managed agent controller for the fixed Next profile."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import herdr_client, next_profile


ALLOWED_KINDS = frozenset({"codex", "claude", "kimi", "opencode", "grok"})

# P1：公共 transcript 的启动段（head）不得泄露 launcher 命令/argv/trust
# override/canonical 路径。launcher echo 只出现在 pane 输出最前几行；对话
# 正文（含用户/Agent 引用的命令与路径）逐字保留，不做全文粗暴过滤。
_LAUNCHER_HEAD_LINES = 8
_TRUST_SIGNATURE = ("projects={", "trust_level=")
_LAUNCHER_PATH_CONTEXT_RE = re.compile(
    r"(?i)working[ _]directory|workdir|cwd", re.UNICODE,
)
_LAUNCHER_CD_RE = re.compile(r"^\s*[$❯>%~]?\s*(?:cd|pushd)\b")
MAX_PROMPT_LENGTH = 16_384
_MAX_REPLAYS = 256
_RETRYABLE_REPLAY = "__retryable_reconcile__"
_PROJECT_ID_RE = re.compile(r"^prj_[0-9a-f]{32}$")
_WORKSPACE_ID_RE = re.compile(r"^ws_[0-9a-f]{32}$")
_LIVE_STATUSES = frozenset({"idle", "working", "blocked", "done"})


class WorkspaceAgentError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _Authority:
    canonical_path: str


@dataclass(frozen=True)
class _LiveAgent:
    pane_id: str
    status: str


class _PromptReceiptStore:
    """Durable idempotency ledger with a committed pre-dispatch state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._initialize()

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = self._open()
            try:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS prompt_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN ('dispatching', 'succeeded', 'failed')
                        ),
                        result_json TEXT,
                        failure_code TEXT,
                        UNIQUE (
                            project_id, workspace_id, agent_id, idempotency_key
                        )
                    );
                """)
            finally:
                connection.close()
            self.path.chmod(0o600)
            self._initialized = True
        except (OSError, sqlite3.Error) as exc:
            raise WorkspaceAgentError("workspace_agent_unavailable") from None

    def reserve(
        self, project_id: str, workspace_id: str, agent_id: str,
        idempotency_key: str, payload_digest: str,
    ) -> dict[str, object] | None:
        receipt_id = self._receipt_id(
            project_id, workspace_id, agent_id, idempotency_key,
        )
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM prompt_receipts WHERE receipt_id=?", (receipt_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO prompt_receipts VALUES (?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        receipt_id, project_id, workspace_id, agent_id,
                        idempotency_key, payload_digest, "dispatching",
                    ),
                )
                connection.commit()
                return None
            if (
                row["project_id"] != project_id
                or row["workspace_id"] != workspace_id
                or row["agent_id"] != agent_id
                or row["idempotency_key"] != idempotency_key
            ):
                raise WorkspaceAgentError("workspace_agent_unavailable")
            if row["payload_digest"] != payload_digest:
                raise WorkspaceAgentError("idempotency_conflict")
            state = row["state"]
            if state == "dispatching":
                raise WorkspaceAgentError("agent_send_outcome_unknown")
            if state == "failed":
                if row["result_json"] is not None:
                    raise WorkspaceAgentError("workspace_agent_unavailable")
                raise WorkspaceAgentError("agent_send_outcome_unknown")
            if state != "succeeded" or row["failure_code"] is not None:
                raise WorkspaceAgentError("workspace_agent_unavailable")
            result = self._decode_result(row["result_json"])
            connection.commit()
            return result
        except WorkspaceAgentError:
            if connection is not None:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            if connection is not None:
                connection.rollback()
            raise WorkspaceAgentError("workspace_agent_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    def complete_success(
        self, project_id: str, workspace_id: str, agent_id: str,
        idempotency_key: str, payload_digest: str, result: dict[str, object],
    ) -> None:
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        self._complete(
            project_id, workspace_id, agent_id, idempotency_key, payload_digest,
            state="succeeded", result_json=encoded, failure_code=None,
        )

    def complete_failure(
        self, project_id: str, workspace_id: str, agent_id: str,
        idempotency_key: str, payload_digest: str, code: str,
    ) -> None:
        self._complete(
            project_id, workspace_id, agent_id, idempotency_key, payload_digest,
            state="failed", result_json=None, failure_code=code,
        )

    def _complete(
        self, project_id: str, workspace_id: str, agent_id: str,
        idempotency_key: str, payload_digest: str, *, state: str,
        result_json: str | None, failure_code: str | None,
    ) -> None:
        receipt_id = self._receipt_id(
            project_id, workspace_id, agent_id, idempotency_key,
        )
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE prompt_receipts SET state=?,result_json=?,failure_code=? "
                "WHERE receipt_id=? AND project_id=? AND workspace_id=? "
                "AND agent_id=? AND idempotency_key=? AND payload_digest=? "
                "AND state='dispatching'",
                (
                    state, result_json, failure_code, receipt_id, project_id,
                    workspace_id, agent_id, idempotency_key, payload_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkspaceAgentError("agent_send_outcome_unknown")
            connection.commit()
        except WorkspaceAgentError:
            if connection is not None:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.rollback()
            raise WorkspaceAgentError("agent_send_outcome_unknown") from None
        finally:
            if connection is not None:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._ensure_initialized()
        return self._open()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=5, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _receipt_id(
        project_id: str, workspace_id: str, agent_id: str, key: str,
    ) -> str:
        encoded = json.dumps(
            ["prompt", project_id, workspace_id, agent_id, key],
            separators=(",", ":"),
        ).encode("utf-8")
        return "war_" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _decode_result(value: object) -> dict[str, object]:
        if not isinstance(value, str):
            raise ValueError("missing result")
        result = json.loads(value)
        fields = {
            "agent_id", "project_id", "workspace_id", "kind", "status",
            "transcript",
        }
        if (
            not isinstance(result, dict)
            or set(result) != fields
            or not all(isinstance(result[field], str) for field in fields)
        ):
            raise ValueError("invalid result")
        return dict(result)


def _default_receipt_path() -> Path:
    return herdr_client.launch_descriptors_path().with_name(
        "workspace-agent-receipts.sqlite3"
    )


class WorkspaceAgentController:
    """Resolve Registry authority before all managed Herdr operations."""

    def __init__(
        self,
        *,
        registry_provider: Callable[[], Any],
        session_provider: Callable[[], str | None] = next_profile.session,
        session_bootstrap_provider: Callable[[str], dict[str, Any]] = (
            herdr_client.ensure_session
        ),
        descriptor_list_provider: Callable[[str, str, str], tuple[dict[str, Any], ...]] = (
            herdr_client.list_workspace_launch_descriptors
        ),
        descriptor_provider: Callable[[str], dict[str, Any] | None] = (
            herdr_client.get_launch_descriptor_by_instance
        ),
        snapshot_provider: Callable[[str], dict[str, Any]] = herdr_client.session_snapshot,
        start_provider: Callable[..., dict[str, Any]] = herdr_client.start_agent,
        send_provider: Callable[..., dict[str, Any]] = herdr_client.pane_send,
        read_provider: Callable[..., dict[str, Any]] = herdr_client.pane_read,
        instance_id_factory: Callable[[], str] = herdr_client.new_agent_instance_id,
        receipt_path: Path | None = None,
        pending_recovery_provider: Callable[[str, str, str], None] = (
            herdr_client.recover_workspace_launch_descriptors
        ),
    ) -> None:
        self._registry_provider = registry_provider
        self._session_provider = session_provider
        self._session_bootstrap_provider = session_bootstrap_provider
        self._descriptor_list_provider = descriptor_list_provider
        self._descriptor_provider = descriptor_provider
        self._snapshot_provider = snapshot_provider
        self._start_provider = start_provider
        self._send_provider = send_provider
        self._read_provider = read_provider
        self._instance_id_factory = instance_id_factory
        self._pending_recovery_provider = pending_recovery_provider
        self._prompt_receipts = _PromptReceiptStore(
            receipt_path or _default_receipt_path()
        )
        self._lock = threading.RLock()
        self._replays: OrderedDict[
            tuple[str, ...], tuple[str, dict[str, object] | str]
        ] = (
            OrderedDict()
        )
        self._closed = False

    def ready(self) -> bool:
        return not self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._replays.clear()

    def start(
        self, project_id: str, workspace_id: str, *, kind: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        self._validate_authority_ids(project_id, workspace_id)
        if not isinstance(kind, str) or kind not in ALLOWED_KINDS:
            raise WorkspaceAgentError("invalid_argument")
        key = self._validate_key(idempotency_key)
        payload = self._digest({"kind": kind})
        replay_key = ("start", project_id, workspace_id, key)
        with self._lock:
            self._require_ready()
            replay = self._replay(replay_key, payload)
            if replay is not None:
                return replay
            self._remember_retryable(replay_key, payload)
            authority = self._authority(project_id, workspace_id)
            session = self._session()
            self._bootstrap(session)
            try:
                self._pending_recovery_provider(session, project_id, workspace_id)
                descriptors = self._descriptor_list_provider(
                    session, project_id, workspace_id,
                )
            except Exception:
                raise WorkspaceAgentError("workspace_agent_unavailable") from None
            if not isinstance(descriptors, (tuple, list)):
                raise WorkspaceAgentError("workspace_agent_unavailable")
            for descriptor in descriptors:
                checked = self._descriptor(
                    descriptor, project_id, workspace_id, session,
                    authority.canonical_path, expected_kind=kind,
                )
                if checked is None:
                    continue
                live = self._live(session, checked["instance_id"], checked["kind"])
                if live is not None:
                    result = self._public(
                        checked, live, self._transcript(
                            session, live.pane_id,
                            canonical_path=authority.canonical_path, kind=checked["kind"],
                        ),
                    )
                    self._remember(replay_key, payload, result)
                    return result

            try:
                agent_id = herdr_client.validate_agent_instance_id(
                    self._instance_id_factory()
                )
                started = self._start_provider(
                    session, authority.canonical_path, kind,
                    layout="tab", label=kind, args="", instance_id=agent_id,
                    project_id=project_id, workspace_id=workspace_id,
                )
            except Exception:
                raise WorkspaceAgentError("agent_start_failed") from None
            if (
                isinstance(started, Mapping)
                and started.get("error_code") == "descriptor_cleanup_incomplete"
            ):
                raise WorkspaceAgentError("agent_start_cleanup_incomplete")
            if (
                not self._provider_success(started)
                or started.get("instance_id") != agent_id
            ):
                raise WorkspaceAgentError("agent_start_failed")
            try:
                descriptor = self._descriptor_provider(agent_id)
            except Exception:
                raise WorkspaceAgentError("agent_start_failed") from None
            checked = self._descriptor(
                descriptor, project_id, workspace_id, session,
                authority.canonical_path, expected_kind=kind,
            )
            if checked is None:
                raise WorkspaceAgentError("agent_start_failed")
            try:
                live = self._live(session, agent_id, kind)
            except WorkspaceAgentError:
                raise WorkspaceAgentError("agent_start_failed") from None
            if live is None:
                raise WorkspaceAgentError("agent_start_failed")
            result = self._public(
                checked, live,
                self._transcript(
                    session, live.pane_id, required=False,
                    canonical_path=authority.canonical_path, kind=checked["kind"],
                ),
            )
            self._remember(replay_key, payload, result)
            return result

    def get(
        self, project_id: str, workspace_id: str, agent_id: str,
    ) -> dict[str, object]:
        self._validate_authority_ids(project_id, workspace_id)
        self._validate_agent_id(agent_id)
        with self._lock:
            self._require_ready()
            authority = self._authority(project_id, workspace_id)
            session = self._session()
            descriptor = self._owned_descriptor(
                project_id, workspace_id, agent_id, session,
                authority.canonical_path,
            )
            live = self._live(session, agent_id, descriptor["kind"])
            if live is None:
                raise WorkspaceAgentError("agent_not_found")
            return self._public(
                descriptor, live, self._transcript(
                    session, live.pane_id,
                    canonical_path=authority.canonical_path, kind=descriptor["kind"],
                ),
            )

    def prompt(
        self, project_id: str, workspace_id: str, agent_id: str, *,
        prompt: object, idempotency_key: object,
    ) -> dict[str, object]:
        self._validate_authority_ids(project_id, workspace_id)
        self._validate_agent_id(agent_id)
        if (
            not isinstance(prompt, str)
            or not prompt
            or len(prompt) > MAX_PROMPT_LENGTH
        ):
            raise WorkspaceAgentError("invalid_argument")
        key = self._validate_key(idempotency_key)
        payload = self._digest({"prompt": prompt})
        with self._lock:
            self._require_ready()
            authority = self._authority(project_id, workspace_id)
            session = self._session()
            descriptor = self._owned_descriptor(
                project_id, workspace_id, agent_id, session,
                authority.canonical_path,
            )
            live = self._live(session, agent_id, descriptor["kind"])
            if live is None:
                raise WorkspaceAgentError("agent_not_found")
            replay = self._prompt_receipts.reserve(
                project_id, workspace_id, agent_id, key, payload,
            )
            if replay is not None:
                return replay
            try:
                sent = self._send_provider(
                    session, live.pane_id, prompt, mode="prompt",
                )
            except Exception:
                self._complete_prompt_unknown(
                    project_id, workspace_id, agent_id, key, payload,
                )
                raise WorkspaceAgentError("agent_send_outcome_unknown") from None
            if not self._provider_success(sent):
                self._complete_prompt_unknown(
                    project_id, workspace_id, agent_id, key, payload,
                )
                raise WorkspaceAgentError("agent_send_outcome_unknown")
            result = self._public(
                descriptor, live,
                self._transcript(
                    session, live.pane_id, required=False,
                    canonical_path=authority.canonical_path, kind=descriptor["kind"],
                ),
            )
            self._prompt_receipts.complete_success(
                project_id, workspace_id, agent_id, key, payload, result,
            )
            return result

    def _authority(self, project_id: str, workspace_id: str) -> _Authority:
        try:
            registry = self._registry_provider()
            project = registry.get_project_by_id(project_id)
            workspace = registry.get_workspace(project_id, workspace_id)
            locations = registry.list_repo_locations(project_id)
        except Exception:
            raise WorkspaceAgentError("workspace_agent_unavailable") from None
        if project is None or workspace is None or locations is None:
            raise WorkspaceAgentError("project_or_workspace_not_found")
        location = next(
            (
                item for item in locations
                if item.repo_location_id == workspace.repo_location_id
            ),
            None,
        )
        if location is None:
            raise WorkspaceAgentError("project_or_workspace_not_found")
        if (
            project.project.lifecycle != "active"
            or workspace.lifecycle != "active"
            or workspace.isolation_kind != "shared"
            or location.lifecycle != "active"
            or location.node_id != "local"
            or location.availability != "available"
            or not isinstance(location.canonical_path, str)
            or not location.canonical_path
        ):
            raise WorkspaceAgentError("workspace_agent_unavailable")
        return _Authority(location.canonical_path)

    def _session(self) -> str:
        try:
            session = self._session_provider()
        except Exception:
            raise WorkspaceAgentError("workspace_agent_unavailable") from None
        if not isinstance(session, str) or not session:
            raise WorkspaceAgentError("workspace_agent_unavailable")
        return session

    def _bootstrap(self, session: str) -> None:
        try:
            result = self._session_bootstrap_provider(session)
        except Exception:
            raise WorkspaceAgentError("workspace_agent_unavailable") from None
        if not self._provider_success(result) or result.get("session") != session:
            if (
                isinstance(result, Mapping)
                and result.get("error_code") == "session_cleanup_incomplete"
            ):
                raise WorkspaceAgentError("workspace_agent_cleanup_incomplete")
            raise WorkspaceAgentError("workspace_agent_unavailable")

    def _complete_prompt_unknown(
        self, project_id: str, workspace_id: str, agent_id: str,
        key: str, payload: str,
    ) -> None:
        try:
            self._prompt_receipts.complete_failure(
                project_id, workspace_id, agent_id, key, payload,
                "agent_send_outcome_unknown",
            )
        except WorkspaceAgentError:
            raise WorkspaceAgentError("agent_send_outcome_unknown") from None

    def _owned_descriptor(
        self, project_id: str, workspace_id: str, agent_id: str,
        session: str, canonical_path: str,
    ) -> dict[str, str]:
        try:
            descriptor = self._descriptor_provider(agent_id)
        except Exception:
            raise WorkspaceAgentError("workspace_agent_unavailable") from None
        checked = self._descriptor(
            descriptor, project_id, workspace_id, session, canonical_path,
        )
        if checked is None:
            raise WorkspaceAgentError("agent_not_found")
        return checked

    @staticmethod
    def _descriptor(
        value: object, project_id: str, workspace_id: str, session: str,
        canonical_path: str, *, expected_kind: str | None = None,
    ) -> dict[str, str] | None:
        if not isinstance(value, Mapping):
            return None
        agent_id = value.get("instance_id")
        kind = value.get("kind")
        if (
            not isinstance(agent_id, str)
            or not re.fullmatch(r"i-[a-z2-7]{26}", agent_id)
            or value.get("name") != agent_id
            or value.get("project_id") != project_id
            or value.get("workspace_id") != workspace_id
            or value.get("session") != session
            or value.get("workdir") != canonical_path
            or value.get("state") != "active"
            or value.get("args") != []
            or not isinstance(kind, str)
            or kind not in ALLOWED_KINDS
            or value.get("agent") != kind
            or (expected_kind is not None and kind != expected_kind)
        ):
            return None
        return {
            "instance_id": agent_id,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "kind": kind,
        }

    def _live(self, session: str, agent_id: str, kind: str) -> _LiveAgent | None:
        try:
            snapshot = self._snapshot_provider(session)
        except Exception:
            raise WorkspaceAgentError("workspace_agent_unavailable") from None
        if not isinstance(snapshot, Mapping) or snapshot.get("session") != session:
            raise WorkspaceAgentError("workspace_agent_unavailable")
        if snapshot.get("error") is not None:
            raise WorkspaceAgentError("workspace_agent_unavailable")
        agents = snapshot.get("agents")
        panes = snapshot.get("panes")
        if not isinstance(agents, list) or not isinstance(panes, list):
            raise WorkspaceAgentError("workspace_agent_unavailable")
        matches = [
            item for item in agents
            if isinstance(item, Mapping) and item.get("name") == agent_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise WorkspaceAgentError("workspace_agent_unavailable")
        live_agent = matches[0]
        pane_id = live_agent.get("pane_id")
        live_kind = live_agent.get("kind", live_agent.get("agent"))
        if not isinstance(pane_id, str) or live_kind != kind:
            raise WorkspaceAgentError("workspace_agent_unavailable")
        pane_matches = [
            item for item in panes
            if isinstance(item, Mapping) and item.get("pane_id") == pane_id
        ]
        if len(pane_matches) != 1:
            raise WorkspaceAgentError("workspace_agent_unavailable")
        pane = pane_matches[0]
        if pane.get("session") not in {None, session} or pane.get("agent") != kind:
            raise WorkspaceAgentError("workspace_agent_unavailable")
        status = pane.get("agent_status")
        if status not in _LIVE_STATUSES:
            status = "unknown"
        return _LiveAgent(pane_id, status)

    def _transcript(
        self, session: str, pane_id: str, *, required: bool = True,
        canonical_path: str | None = None, kind: str | None = None,
    ) -> str:
        try:
            value = self._read_provider(session, pane_id, lines=100, is_agent=True)
        except Exception:
            if not required:
                return ""
            raise WorkspaceAgentError("workspace_agent_unavailable") from None
        if not self._provider_success(value) or not isinstance(value.get("output"), str):
            if not required:
                return ""
            raise WorkspaceAgentError("workspace_agent_unavailable")
        return self._scrub_launcher_head(value["output"], canonical_path, kind)

    @staticmethod
    def _scrub_launcher_head(
        text: str, canonical_path: str | None, kind: str | None,
    ) -> str:
        """Drop launcher-echo lines from the transcript head only.

        启动 echo/banner 只出现在输出最前 _LAUNCHER_HEAD_LINES 行；命中规则：
        含 trust override 签名（服务端专属 token）、canonical 路径与 kind/
        目录字样同时出现、或 cd <path> 形态。对话正文不过滤，用户与 Agent
        回复中的命令、路径逐字保留（增量轮询/刷新恢复走同一路径）。
        """
        if not text:
            return text
        path = (
            canonical_path
            if isinstance(canonical_path, str) and canonical_path
            else None
        )
        kind_re = re.compile(r"\b" + re.escape(kind) + r"\b") if kind else None
        lines = text.split("\n")
        head, body = lines[:_LAUNCHER_HEAD_LINES], lines[_LAUNCHER_HEAD_LINES:]
        kept: list[str] = []
        for line in head:
            if any(signature in line for signature in _TRUST_SIGNATURE):
                continue
            if path and path in line and (
                (kind_re is not None and kind_re.search(line) is not None)
                or _LAUNCHER_PATH_CONTEXT_RE.search(line) is not None
                or _LAUNCHER_CD_RE.match(line) is not None
            ):
                continue
            kept.append(line)
        return "\n".join(kept + body)

    @staticmethod
    def _provider_success(value: object) -> bool:
        return (
            isinstance(value, Mapping)
            and value.get("available") is True
            and "error" not in value
            and "error_code" not in value
            and "descriptor_error" not in value
        )

    @staticmethod
    def _public(
        descriptor: Mapping[str, str], live: _LiveAgent, transcript: str,
    ) -> dict[str, object]:
        return {
            "agent_id": descriptor["instance_id"],
            "project_id": descriptor["project_id"],
            "workspace_id": descriptor["workspace_id"],
            "kind": descriptor["kind"],
            "status": live.status,
            "transcript": transcript,
        }

    def _replay(
        self, key: tuple[str, ...], payload: str,
    ) -> dict[str, object] | None:
        current = self._replays.get(key)
        if current is None:
            return None
        if current[0] != payload:
            raise WorkspaceAgentError("idempotency_conflict")
        self._replays.move_to_end(key)
        if current[1] == _RETRYABLE_REPLAY:
            return None
        if isinstance(current[1], str):
            raise WorkspaceAgentError(current[1])
        return dict(current[1])

    def _remember(
        self, key: tuple[str, ...], payload: str, result: dict[str, object],
    ) -> None:
        self._replays[key] = (payload, dict(result))
        self._replays.move_to_end(key)
        while len(self._replays) > _MAX_REPLAYS:
            self._replays.popitem(last=False)

    def _remember_retryable(
        self, key: tuple[str, ...], payload: str,
    ) -> None:
        self._replays[key] = (payload, _RETRYABLE_REPLAY)
        self._replays.move_to_end(key)
        while len(self._replays) > _MAX_REPLAYS:
            self._replays.popitem(last=False)

    @staticmethod
    def _digest(value: Mapping[str, object]) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_key(value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
        ):
            raise WorkspaceAgentError("idempotency_key_required")
        return value

    @staticmethod
    def _validate_authority_ids(project_id: object, workspace_id: object) -> None:
        if (
            not isinstance(project_id, str)
            or _PROJECT_ID_RE.fullmatch(project_id) is None
            or not isinstance(workspace_id, str)
            or _WORKSPACE_ID_RE.fullmatch(workspace_id) is None
        ):
            raise WorkspaceAgentError("invalid_argument")

    @staticmethod
    def _validate_agent_id(agent_id: object) -> None:
        try:
            herdr_client.validate_agent_instance_id(agent_id)
        except ValueError:
            raise WorkspaceAgentError("invalid_argument") from None

    def _require_ready(self) -> None:
        if self._closed:
            raise WorkspaceAgentError("workspace_agent_unavailable")
