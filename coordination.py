"""Agent Mail 可靠消费与协作式暂停/恢复的本地 sidecar。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import runtime_paths

# 可插拔控制消息认领门（B0 W6）：默认 None 保持历史行为；由接线层安装。
CONTROL_CLAIM_GATE: Any = None



DB_PATH = runtime_paths.store("coordination")
CLAIM_TTL = 300.0
META_PREFIX = "<!-- agent-cockpit-meta:"
META_SUFFIX = " -->"
INTENTS = {"info", "action", "review", "blocking", "stop", "redirect"}
INTERRUPT_INTENTS = {"review", "blocking", "stop", "redirect"}
NO_RESUME_INTENTS = {"stop", "redirect"}
CONNECT_RETRIES = 6
CONNECT_RETRY_BASE = 0.02
TASK_REPORT_TEXT_LIMIT = 2000
_CONNECT_INIT_LOCK = threading.Lock()


def message_timestamp(message: dict[str, Any], default: float = 0.0) -> float:
    """兼容 Hub 的 epoch created_ts 与 ISO-8601 created_at。"""
    value = message.get("created_ts") or message.get("created_at")
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return default


def message_state_revision() -> tuple[tuple[int, int], ...]:
    """协调库及 WAL 的轻量版本；文件变化时调用方再查询 receipt 签名。"""
    revisions = []
    for path in (DB_PATH, Path(str(DB_PATH) + "-wal")):
        try:
            stat = path.stat()
            revisions.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            revisions.append((0, 0))
    return tuple(revisions)


def message_project_signatures() -> dict[str, tuple[tuple[Any, ...], ...]]:
    """按项目返回不含正文的 receipt 行签名，供消息页revision检测。"""
    if not DB_PATH.is_file():
        return {}
    with _connect() as con:
        rows = con.execute(
            "SELECT project_key,recipient,message_id,state,reason,ack_pending,updated_ts "
            "FROM receipts ORDER BY project_key,recipient,message_id"
        ).fetchall()
    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        key = str(Path(row["project_key"]).expanduser().resolve())
        grouped.setdefault(key, []).append((
            row["recipient"], row["message_id"], row["state"], row["reason"],
            row["ack_pending"], row["updated_ts"],
        ))
    return {key: tuple(values) for key, values in grouped.items()}


def _initialize_connection(con: sqlite3.Connection) -> None:
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY,
          project_key TEXT NOT NULL,
          session TEXT NOT NULL,
          session_dir TEXT NOT NULL,
          revision INTEGER NOT NULL,
          state TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          started_ts REAL NOT NULL,
          closed_ts REAL,
          UNIQUE(session, session_dir, revision)
        );
        CREATE INDEX IF NOT EXISTS runs_project_state
          ON runs(project_key, state);
        CREATE TABLE IF NOT EXISTS participants (
          run_id TEXT NOT NULL REFERENCES runs(run_id),
          participant_id TEXT NOT NULL,
          agent_type TEXT NOT NULL,
          mail_name TEXT,
          pane_id TEXT,
          role TEXT NOT NULL,
          task_text TEXT NOT NULL,
          task_revision INTEGER NOT NULL,
          workdir TEXT NOT NULL,
          state TEXT NOT NULL,
          updated_ts REAL NOT NULL,
          PRIMARY KEY(run_id, participant_id)
        );
        CREATE INDEX IF NOT EXISTS participants_mail
          ON participants(mail_name, run_id);
        CREATE TABLE IF NOT EXISTS message_meta (
          project_key TEXT NOT NULL,
          message_id INTEGER NOT NULL,
          sender TEXT NOT NULL,
          meta_json TEXT NOT NULL,
          trusted_user INTEGER NOT NULL DEFAULT 0,
          created_ts REAL NOT NULL,
          PRIMARY KEY(project_key, message_id)
        );
        CREATE TABLE IF NOT EXISTS receipts (
          project_key TEXT NOT NULL,
          recipient TEXT NOT NULL,
          message_id INTEGER NOT NULL,
          sender TEXT,
          run_id TEXT,
          task_id TEXT,
          task_revision INTEGER,
          intent TEXT NOT NULL,
          importance TEXT NOT NULL,
          state TEXT NOT NULL,
          claim_owner TEXT,
          claim_token TEXT,
          claim_expires_ts REAL,
          reason TEXT,
          checkpoint_json TEXT,
          ack_pending INTEGER NOT NULL DEFAULT 0,
          created_ts REAL NOT NULL,
          updated_ts REAL NOT NULL,
          PRIMARY KEY(project_key, recipient, message_id)
        );
        CREATE INDEX IF NOT EXISTS receipts_claims
          ON receipts(state, claim_expires_ts);
        CREATE TABLE IF NOT EXISTS task_reports (
          session TEXT NOT NULL,
          pane_id TEXT NOT NULL,
          agent_type TEXT NOT NULL,
          mail_name TEXT,
          request_id TEXT NOT NULL,
          requested_ts REAL NOT NULL,
          request_error TEXT,
          report_request_id TEXT,
          progress INTEGER,
          summary TEXT,
          next_step TEXT,
          blocker TEXT,
          reported_ts REAL,
          PRIMARY KEY(session, pane_id)
        );
        """
    )
    columns = {
        row["name"] for row in con.execute("PRAGMA table_info(receipts)").fetchall()
    }
    if "claim_token" not in columns:
        try:
            con.execute("ALTER TABLE receipts ADD COLUMN claim_token TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def _connect() -> sqlite3.Connection:
    runtime_paths.validate_store("coordination")  # R3-B:symlink 逃逸 fail-closed
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    delay = CONNECT_RETRY_BASE
    for attempt in range(CONNECT_RETRIES):
        con = None
        try:
            con = sqlite3.connect(DB_PATH, timeout=5)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=5000")
            with _CONNECT_INIT_LOCK:
                _initialize_connection(con)
            return con
        except sqlite3.OperationalError as exc:
            if con is not None:
                con.close()
            if "locked" not in str(exc).lower() or attempt == CONNECT_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("coordination DB 连接重试耗尽")


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _config_hash(participants: list[dict[str, Any]]) -> str:
    config = [
        {
            "id": str(item["id"]),
            "agent": str(item["agent"]),
            "role": str(item["role"]),
            "task": str(item["task"]),
            "workdir": str(Path(item["workdir"]).expanduser().resolve()),
        }
        for item in participants
    ]
    return hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def start_run(
    *,
    project_key: str,
    session: str,
    session_dir: str,
    participants: list[dict[str, Any]],
    now: float | None = None,
) -> dict[str, Any]:
    """创建新 run；完全相同的活动配置幂等复用。"""
    if not participants:
        raise ValueError("run 至少需要一个参与者")
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    generation = str(Path(session_dir).expanduser().resolve())
    digest = _config_hash(participants)
    con = _connect()
    try:
        # BEGIN IMMEDIATE 在读取 active/MAX revision 前拿下写锁，保证同
        # (session, session_dir) 并发 start_run 串行——否则两个事务都读到
        # 相同 MAX(revision)，第二个 INSERT 撞 UNIQUE(session,session_dir,revision)。
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            "SELECT * FROM runs WHERE session=? AND session_dir=? AND state='active' "
            "ORDER BY revision DESC LIMIT 1",
            (session, generation),
        ).fetchone()
        if active is not None and active["config_hash"] == digest:
            run_id = str(active["run_id"])
            for item in participants:
                con.execute(
                    "UPDATE participants SET pane_id=COALESCE(?,pane_id), "
                    "mail_name=COALESCE(?,mail_name), updated_ts=? "
                    "WHERE run_id=? AND participant_id=?",
                    (
                        item.get("pane_id"), item.get("mail_name"), current,
                        run_id, str(item["id"]),
                    ),
                )
            con.commit()
            result = dict(active)
            result.update({"created": False, "reused": True})
            return result
        if active is not None:
            con.execute(
                "UPDATE runs SET state='superseded', closed_ts=? WHERE run_id=?",
                (current, active["run_id"]),
            )
            con.execute(
                "UPDATE participants SET state='superseded', updated_ts=? "
                "WHERE run_id=?",
                (current, active["run_id"]),
            )
        row = con.execute(
            "SELECT COALESCE(MAX(revision),0)+1 AS revision FROM runs "
            "WHERE session=? AND session_dir=?",
            (session, generation),
        ).fetchone()
        revision = int(row["revision"])
        run_id = uuid.uuid4().hex
        con.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,'active',?,?,NULL)",
            (run_id, project, session, generation, revision, digest, current),
        )
        for item in participants:
            con.execute(
                "INSERT INTO participants VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, str(item["id"]), str(item["agent"]),
                    item.get("mail_name"), item.get("pane_id"), str(item["role"]),
                    str(item["task"]), int(item.get("task_revision", 1)),
                    str(Path(item["workdir"]).expanduser().resolve()),
                    "working", current,
                ),
            )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    return {
        "run_id": run_id, "project_key": project, "session": session,
        "session_dir": generation, "revision": revision, "state": "active",
        "started_ts": current, "created": True, "reused": False,
    }


def bind_identity(
    run_id: str, participant_id: str, mail_name: str, pane_id: str | None = None,
    *, now: float | None = None,
) -> bool:
    current = time.time() if now is None else now
    with _connect() as con:
        cur = con.execute(
            "UPDATE participants SET mail_name=?, pane_id=COALESCE(?,pane_id), "
            "updated_ts=? WHERE run_id=? AND participant_id=?",
            (mail_name, pane_id, current, run_id, participant_id),
        )
        return cur.rowcount == 1


def add_participant(
    *, session: str, participant_id: str, agent: str, pane_id: str,
    workdir: str, mail_name: str | None = None, role: str = "developer",
    task: str = "", now: float | None = None,
) -> dict[str, Any]:
    """把运行中新增的 pane 纳入当前 run，不打断已有参与者。"""
    if not all(str(value).strip() for value in (session, participant_id, agent, pane_id, workdir)):
        raise ValueError("新增参与者缺少 session/id/agent/pane/workdir")
    current = time.time() if now is None else now
    resolved_workdir = str(Path(workdir).expanduser().resolve())
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        active = con.execute(
            "SELECT * FROM runs WHERE session=? AND state='active' "
            "ORDER BY revision DESC LIMIT 1",
            (session,),
        ).fetchone()
        if active is None:
            con.commit()
            return {"joined": False, "reused": False, "reason": "no_active_run"}
        run_id = str(active["run_id"])
        existing_pane = con.execute(
            "SELECT * FROM participants WHERE run_id=? AND pane_id=?",
            (run_id, pane_id),
        ).fetchone()
        existing_id = con.execute(
            "SELECT * FROM participants WHERE run_id=? AND participant_id=?",
            (run_id, participant_id),
        ).fetchone()
        if existing_id is not None and (
            existing_pane is None
            or existing_id["participant_id"] != existing_pane["participant_id"]
        ):
            raise ValueError(f"参与者 id 已存在: {participant_id}")
        existing = existing_pane or existing_id
        if existing is not None:
            actual_id = str(existing["participant_id"])
            if str(existing["agent_type"]) != agent:
                raise ValueError(f"pane {pane_id} 已绑定其他 Agent")
            con.execute(
                "UPDATE participants SET mail_name=COALESCE(?,mail_name),workdir=?,"
                "updated_ts=? WHERE run_id=? AND participant_id=?",
                (mail_name, resolved_workdir, current, run_id, actual_id),
            )
            reused = True
        else:
            actual_id = participant_id
            con.execute(
                "INSERT INTO participants VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, actual_id, agent, mail_name, pane_id, role, task, 1,
                    resolved_workdir, "working", current,
                ),
            )
            reused = False
        rows = con.execute(
            "SELECT participant_id,agent_type,role,task_text,workdir "
            "FROM participants WHERE run_id=? ORDER BY participant_id",
            (run_id,),
        ).fetchall()
        digest = _config_hash([
            {
                "id": row["participant_id"], "agent": row["agent_type"],
                "role": row["role"], "task": row["task_text"],
                "workdir": row["workdir"],
            }
            for row in rows
        ])
        con.execute("UPDATE runs SET config_hash=? WHERE run_id=?", (digest, run_id))
        con.commit()
        return {
            "joined": True, "reused": reused, "run_id": run_id,
            "participant_id": actual_id, "project_key": str(active["project_key"]),
            "run_revision": int(active["revision"]), "task_revision": 1,
        }
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def run_participants(run_id: str) -> list[dict[str, Any]]:
    with _connect() as con:
        return [
            dict(row) for row in con.execute(
                "SELECT * FROM participants WHERE run_id=? ORDER BY participant_id",
                (run_id,),
            ).fetchall()
        ]


def panes_by_mail_name(project_key: str, mail_name: str) -> dict[str, set]:
    """某项目内某 mail 花名强绑定的 session → {pane_id}（消息通知一级选路用）。

    花名是项目内身份：必须限定 project_key，避免把本项目消息投到另一项目
    同花名的 pane。project_key 需为 canonical（expanduser+resolve）形式。
    """
    if not mail_name:
        return {}
    project = str(Path(project_key).expanduser().resolve())
    with _connect() as con:
        rows = con.execute(
            "SELECT r.session, p.pane_id FROM runs r "
            "JOIN participants p ON p.run_id = r.run_id "
            "WHERE r.state='active' AND r.project_key = ? AND p.mail_name = ?",
            (project, mail_name),
        ).fetchall()
    out: dict[str, set] = {}
    for row in rows:
        session = str(row["session"] or "")
        pane_id = str(row["pane_id"] or "")
        if session and pane_id:
            out.setdefault(session, set()).add(pane_id)
    return out


def active_context(project_key: str, mail_name: str) -> dict[str, Any] | None:
    project = str(Path(project_key).expanduser().resolve())
    with _connect() as con:
        rows = con.execute(
            "SELECT r.*,p.participant_id,p.agent_type,p.mail_name,p.pane_id,p.role,"
            "p.task_text,p.task_revision,p.workdir,p.state AS participant_state "
            "FROM runs r JOIN participants p ON p.run_id=r.run_id "
            "WHERE r.project_key=? AND r.state='active' AND p.mail_name=?",
            (project, mail_name),
        ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def run_context(run_id: str, mail_name: str) -> dict[str, Any] | None:
    with _connect() as con:
        return _dict(con.execute(
            "SELECT r.*,p.participant_id,p.agent_type,p.mail_name,p.pane_id,p.role,"
            "p.task_text,p.task_revision,p.workdir,p.state AS participant_state "
            "FROM runs r JOIN participants p ON p.run_id=r.run_id "
            "WHERE r.run_id=? AND p.mail_name=?",
            (run_id, mail_name),
        ).fetchone())


def prepare_metadata(
    *,
    project_key: str,
    sender: str,
    recipients: list[str],
    intent: str = "info",
    importance: str = "normal",
    authority: str = "agent",
    supersedes: list[int] | None = None,
    expires_in: float | None = None,
    now: float | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if intent not in INTENTS:
        raise ValueError(f"非法消息 intent: {intent}")
    current = time.time() if now is None else now
    context = active_context(project_key, sender)
    warnings: list[str] = []
    effective = intent
    role = str((context or {}).get("role") or "unknown")
    if intent in NO_RESUME_INTENTS and authority != "user" and role != "lead":
        effective = "blocking"
        warnings.append(f"{role} 无权发送 {intent}，已降级为 blocking")
    meta: dict[str, Any] = {
        "v": 1, "intent": effective, "importance": importance,
        "sender": sender, "sender_role": role, "authority": authority,
        "supersedes": [int(value) for value in (supersedes or [])],
    }
    if expires_in is not None:
        if effective not in ("info", "review"):
            raise ValueError("只有 info/review 临时消息允许设置 expires_in")
        if expires_in <= 0:
            raise ValueError("expires_in 必须大于 0")
        meta["expires_at"] = current + float(expires_in)
    if context:
        meta.update({
            "run_id": context["run_id"], "run_revision": context["revision"],
            "targets": {},
        })
        with _connect() as con:
            for recipient in recipients:
                target = con.execute(
                    "SELECT participant_id,task_revision FROM participants "
                    "WHERE run_id=? AND mail_name=?",
                    (context["run_id"], recipient),
                ).fetchone()
                if target:
                    meta["targets"][recipient] = {
                        "task_id": target["participant_id"],
                        "task_revision": target["task_revision"],
                    }
    return meta, warnings


def add_metadata(body: str, meta: dict[str, Any]) -> str:
    clean, _ = parse_metadata(body)
    encoded = json.dumps(meta, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{META_PREFIX}{encoded}{META_SUFFIX}\n{clean}".rstrip()


def parse_metadata(body: str) -> tuple[str, dict[str, Any] | None]:
    text = body or ""
    if not text.startswith(META_PREFIX):
        return text, None
    end = text.find(META_SUFFIX, len(META_PREFIX))
    if end < 0:
        return text, None
    try:
        meta = json.loads(text[len(META_PREFIX):end])
    except (TypeError, ValueError):
        return text, None
    clean = text[end + len(META_SUFFIX):]
    if clean.startswith("\n"):
        clean = clean[1:]
    return clean, meta if isinstance(meta, dict) else None


def register_message(
    *, project_key: str, message_id: int, sender: str, meta: dict[str, Any],
    trusted_user: bool = False, now: float | None = None,
) -> None:
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    with _connect() as con:
        con.execute(
            "INSERT INTO message_meta VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(project_key,message_id) DO UPDATE SET "
            "sender=excluded.sender,meta_json=excluded.meta_json,"
            "trusted_user=MAX(message_meta.trusted_user,excluded.trusted_user)",
            (
                project, int(message_id), sender,
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
                int(trusted_user), current,
            ),
        )


def _trusted_metadata(
    con: sqlite3.Connection, project: str, message: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, bool]:
    clean, embedded = parse_metadata(str(message.get("body_md") or ""))
    row = con.execute(
        "SELECT * FROM message_meta WHERE project_key=? AND message_id=?",
        (project, int(message["id"])),
    ).fetchone()
    if row:
        try:
            stored = json.loads(row["meta_json"])
        except ValueError:
            stored = None
        if isinstance(stored, dict):
            return clean, stored, bool(row["trusted_user"])
    if isinstance(embedded, dict):
        embedded = dict(embedded)
        embedded["authority"] = "agent"
        return clean, embedded, False
    return clean, None, False


def observe_messages(
    project_key: str, recipient: str, messages: list[dict[str, Any]],
    *, now: float | None = None,
) -> None:
    """先应用整批 supersedes，避免同批先执行已被后信替代的旧消息。"""
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    batch = {int(message["id"]): message for message in messages}
    with _connect() as con:
        for message in messages:
            _, meta, trusted_user = _trusted_metadata(con, project, message)
            sender = str(message.get("from") or message.get("sender_name") or "")
            run_id = (meta or {}).get("run_id")
            sender_row = con.execute(
                "SELECT role FROM participants WHERE run_id=? AND mail_name=?",
                (run_id, sender),
            ).fetchone() if run_id else None
            for old_id in (meta or {}).get("supersedes") or []:
                try:
                    old = int(old_id)
                except (TypeError, ValueError):
                    continue
                old_sender = None
                if old in batch:
                    old_sender = str(
                        batch[old].get("from") or batch[old].get("sender_name") or ""
                    )
                if old_sender is None:
                    old_meta = con.execute(
                        "SELECT sender FROM message_meta WHERE project_key=? AND message_id=?",
                        (project, old),
                    ).fetchone()
                    old_sender = str(old_meta["sender"]) if old_meta else None
                authorized = (
                    trusted_user or (sender_row and sender_row["role"] == "lead")
                    or (old_sender is not None and old_sender == sender)
                )
                if not authorized:
                    continue
                con.execute(
                    "INSERT INTO receipts(project_key,recipient,message_id,intent,importance,"
                    "state,reason,ack_pending,created_ts,updated_ts) "
                    "VALUES(?,?,?,?,?,'stale',?,1,?,?) "
                    "ON CONFLICT(project_key,recipient,message_id) DO UPDATE SET "
                    "state=CASE WHEN receipts.state='processed' THEN receipts.state ELSE 'stale' END,"
                    "reason=CASE WHEN receipts.state='processed' THEN receipts.reason ELSE excluded.reason END,"
                    "ack_pending=CASE WHEN receipts.state='processed' THEN receipts.ack_pending ELSE 1 END,"
                    "updated_ts=excluded.updated_ts",
                    (
                        project, recipient, old, "info", "normal",
                        f"superseded_by:{message['id']}", current, current,
                    ),
                )


def _automatic_checkpoint(cwd: str) -> dict[str, Any]:
    path = str(Path(cwd).expanduser().resolve())
    checkpoint: dict[str, Any] = {
        "cwd": path, "captured_ts": time.time(), "step_state": "safe_point",
    }
    try:
        root = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if root.returncode != 0:
            return checkpoint
        checkpoint["git_root"] = root.stdout.strip()
        head = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        status = subprocess.run(
            ["git", "-C", path, "status", "--short"],
            capture_output=True, text=True, timeout=3,
        )
        checkpoint["git_head"] = head.stdout.strip() if head.returncode == 0 else None
        checkpoint["git_status"] = status.stdout[-8000:] if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        checkpoint["git_probe_failed"] = True
    return checkpoint


def claim_message(
    *, project_key: str, recipient: str, message: dict[str, Any], claimant: str,
    cwd: str, now: float | None = None, ttl: float = CLAIM_TTL,
) -> dict[str, Any]:
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    checkpoint = _automatic_checkpoint(cwd)
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT * FROM receipts WHERE project_key=? AND recipient=? AND message_id=?",
            (project, recipient, int(message["id"])),
        ).fetchone()
        if existing and existing["state"] in ("processed", "stale"):
            con.commit()
            return {"deliver": False, **dict(existing)}
        if (
            existing and existing["state"] == "claimed"
            and float(existing["claim_expires_ts"] or 0) > current
        ):
            con.commit()
            return {"deliver": False, **dict(existing)}
        clean, meta, trusted_user = _trusted_metadata(con, project, message)
        sender = str(message.get("from") or message.get("sender_name") or "")
        importance = str(message.get("importance") or "normal")
        intent = str((meta or {}).get("intent") or "info")
        # 可插拔控制消息认领门（B0 W6 服务端 fail-closed；默认 None 旧行为）。
        # 钩子签名: (project, recipient, message, meta) -> (ok, reason)
        if CONTROL_CLAIM_GATE is not None:
            try:
                gate_ok, gate_reason = CONTROL_CLAIM_GATE(
                    project, recipient, message, meta,
                )
            except Exception:
                gate_ok, gate_reason = False, "claim_gate_error"
            if not gate_ok:
                reason = str(gate_reason or "claim_rejected")
                con.execute(
                    "INSERT INTO receipts(project_key,recipient,message_id,"
                    "sender,intent,importance,state,reason,ack_pending,"
                    "created_ts,updated_ts) "
                    "VALUES(?,?,?,?,?,?,'stale',?,1,?,?) "
                    "ON CONFLICT(project_key,recipient,message_id) DO UPDATE SET "
                    "state=CASE WHEN receipts.state='processed' "
                    "THEN receipts.state ELSE 'stale' END,"
                    "reason=CASE WHEN receipts.state='processed' "
                    "THEN receipts.reason ELSE excluded.reason END,"
                    "updated_ts=excluded.updated_ts",
                    (project, recipient, int(message["id"]), sender,
                     intent, importance, reason, current, current),
                )
                con.commit()
                return {
                    "deliver": False, "reason": reason,
                    "message_id": int(message["id"]),
                }
        claim_token = uuid.uuid4().hex
        run_id = (meta or {}).get("run_id")
        task_id = None
        task_revision = None
        stale_reason = None
        context = None
        if run_id:
            context = _dict(con.execute(
                "SELECT r.*,p.participant_id,p.role,p.task_revision,p.state AS participant_state "
                "FROM runs r JOIN participants p ON p.run_id=r.run_id "
                "WHERE r.run_id=? AND r.project_key=? AND p.mail_name=?",
                (run_id, project, recipient),
            ).fetchone())
            target = ((meta or {}).get("targets") or {}).get(recipient) or {}
            task_id = target.get("task_id")
            task_revision = target.get("task_revision")
            if not context or context["state"] != "active":
                stale_reason = "run_not_active"
            elif task_id != context["participant_id"]:
                stale_reason = "task_mismatch"
            elif int(task_revision or -1) != int(context["task_revision"]):
                stale_reason = "revision_mismatch"
            actual_sender = _dict(con.execute(
                "SELECT p.role FROM participants p WHERE p.run_id=? AND p.mail_name=?",
                (run_id, sender),
            ).fetchone())
            sender_role = str((actual_sender or {}).get("role") or "unknown")
            if intent in INTERRUPT_INTENTS and not actual_sender and not trusted_user:
                intent = "info"
            elif intent in NO_RESUME_INTENTS and not trusted_user and sender_role != "lead":
                intent = "blocking"
        else:
            rows = con.execute(
                "SELECT r.*,p.participant_id,p.task_revision,p.state AS participant_state "
                "FROM runs r JOIN participants p ON p.run_id=r.run_id "
                "WHERE r.project_key=? AND r.state='active' AND p.mail_name=?",
                (project, recipient),
            ).fetchall()
            if len(rows) == 1:
                context = dict(rows[0])
                run_id = context["run_id"]
                task_id = context["participant_id"]
                task_revision = context["task_revision"]
                if intent in INTERRUPT_INTENTS and not trusted_user:
                    intent = "info"
                created = message_timestamp(message)
                if created and created < float(context["started_ts"]):
                    stale_reason = "legacy_before_run"
        expires_at = (meta or {}).get("expires_at")
        if expires_at is not None:
            try:
                if current > float(expires_at):
                    stale_reason = "expired"
            except (TypeError, ValueError):
                stale_reason = "invalid_expiry"
        state = "stale" if stale_reason else "claimed"
        checkpoint_json = (
            existing["checkpoint_json"]
            if existing and existing["checkpoint_json"]
            else json.dumps(checkpoint, ensure_ascii=False)
        )
        con.execute(
            "INSERT INTO receipts(project_key,recipient,message_id,sender,run_id,task_id,"
            "task_revision,intent,importance,state,claim_owner,claim_token,claim_expires_ts,"
            "reason,checkpoint_json,ack_pending,created_ts,updated_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_key,recipient,message_id) DO UPDATE SET "
            "sender=excluded.sender,run_id=excluded.run_id,task_id=excluded.task_id,"
            "task_revision=excluded.task_revision,intent=excluded.intent,"
            "importance=excluded.importance,state=excluded.state,"
            "claim_owner=excluded.claim_owner,claim_token=excluded.claim_token,"
            "claim_expires_ts=excluded.claim_expires_ts,"
            "reason=excluded.reason,checkpoint_json=excluded.checkpoint_json,"
            "ack_pending=excluded.ack_pending,updated_ts=excluded.updated_ts",
            (
                project, recipient, int(message["id"]), sender, run_id, task_id,
                task_revision, intent, importance, state,
                claimant if not stale_reason else None,
                claim_token if not stale_reason else None,
                current + ttl if not stale_reason else None,
                stale_reason, checkpoint_json, int(bool(stale_reason)),
                message_timestamp(message, current), current,
            ),
        )
        if context and not stale_reason and intent in INTERRUPT_INTENTS:
            con.execute(
                "UPDATE participants SET state='handling_interrupt',updated_ts=? "
                "WHERE run_id=? AND participant_id=?",
                (current, run_id, task_id),
            )
        row = con.execute(
            "SELECT * FROM receipts WHERE project_key=? AND recipient=? AND message_id=?",
            (project, recipient, int(message["id"])),
        ).fetchone()
        con.commit()
        return {
            "deliver": not stale_reason, "body_md": clean, "meta": meta,
            **dict(row),
        }
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def checkpoint_message(
    project_key: str, recipient: str, message_id: int, *, summary: str = "",
    next_step: str = "", in_flight: str = "", safe: bool = True,
    claim_token: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    con = _connect()
    try:
        # lease 校验与 checkpoint 写入置于同一 BEGIN IMMEDIATE 事务，阻止旧
        # claim_token 在过期→reclaim 后仍覆盖新持有者的检查点。
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM receipts WHERE project_key=? AND recipient=? AND message_id=?",
            (project, recipient, int(message_id)),
        ).fetchone()
        if not row:
            raise ValueError(f"消息 #{message_id} 尚未 claim")
        if row["state"] != "claimed" and row["state"] != "processed":
            # pending(watcher 回收的过期 claim)/stale/failed 等状态一律拒绝，
            # 阻止过期 lease 在 reclaim 窗口仍覆盖 checkpoint_json。
            raise ValueError(
                f"消息 #{message_id} 当前状态为 {row['state']}，不可写检查点"
            )
        if row["state"] == "claimed":
            if (
                row["claim_expires_ts"] is not None
                and float(row["claim_expires_ts"]) <= current
            ):
                raise ValueError(f"消息 #{message_id} 的 claim 已过期")
            if row["claim_token"] and claim_token != row["claim_token"]:
                raise ValueError(f"消息 #{message_id} 的 claim 已失效")
        try:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
        except ValueError:
            checkpoint = {}
        checkpoint.update({
            "summary": summary, "next_step": next_step, "in_flight": in_flight,
            "in_flight_safe": bool(safe), "updated_ts": current,
            "step_state": "verified" if safe else "uncertain",
        })
        con.execute(
            "UPDATE receipts SET checkpoint_json=?,updated_ts=? "
            "WHERE project_key=? AND recipient=? AND message_id=?",
            (json.dumps(checkpoint, ensure_ascii=False), current, project, recipient, message_id),
        )
        con.commit()
        return checkpoint
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def request_pause(
    *, project_key: str, recipient: str, message_id: int, cwd: str,
    hard: bool = False, now: float | None = None,
) -> dict[str, Any] | None:
    """由 Cockpit 在投递 interrupt 前保存外部 checkpoint 并标记暂停请求。"""
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    checkpoint = _automatic_checkpoint(cwd)
    checkpoint["step_state"] = "uncertain" if hard else "safe_point_requested"
    with _connect() as con:
        meta_row = con.execute(
            "SELECT meta_json,sender FROM message_meta WHERE project_key=? AND message_id=?",
            (project, int(message_id)),
        ).fetchone()
        if not meta_row:
            return None
        try:
            meta = json.loads(meta_row["meta_json"])
        except ValueError:
            return None
        run_id = meta.get("run_id")
        target = (meta.get("targets") or {}).get(recipient) or {}
        task_id = target.get("task_id")
        task_revision = target.get("task_revision")
        intent = str(meta.get("intent") or "info")
        if not run_id or not task_id or intent not in INTERRUPT_INTENTS:
            return None
        context = con.execute(
            "SELECT r.state,p.task_revision,p.workdir FROM runs r "
            "JOIN participants p ON p.run_id=r.run_id WHERE r.run_id=? "
            "AND p.participant_id=? AND p.mail_name=?",
            (run_id, task_id, recipient),
        ).fetchone()
        if (
            not context or context["state"] != "active"
            or int(context["task_revision"]) != int(task_revision or -1)
        ):
            return None
        con.execute(
            "INSERT INTO receipts(project_key,recipient,message_id,sender,run_id,task_id,"
            "task_revision,intent,importance,state,checkpoint_json,created_ts,updated_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?) "
            "ON CONFLICT(project_key,recipient,message_id) DO UPDATE SET "
            "checkpoint_json=excluded.checkpoint_json,updated_ts=excluded.updated_ts",
            (
                project, recipient, int(message_id), meta_row["sender"], run_id,
                task_id, task_revision, intent, str(meta.get("importance") or "normal"),
                json.dumps(checkpoint, ensure_ascii=False), current, current,
            ),
        )
        con.execute(
            "UPDATE participants SET state='pause_requested',updated_ts=? "
            "WHERE run_id=? AND participant_id=?",
            (current, run_id, task_id),
        )
    return checkpoint


def dismiss_message(
    project_key: str, recipient: str, message_id: int, reason: str = "user_ack",
    *, now: float | None = None,
) -> None:
    """人在 UI 中确认后归档，防止同一消息随后又被 Agent 执行。"""
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    with _connect() as con:
        con.execute(
            "INSERT INTO receipts(project_key,recipient,message_id,intent,importance,state,"
            "reason,ack_pending,created_ts,updated_ts) "
            "VALUES(?,?,?,'info','normal','stale',?,1,?,?) "
            "ON CONFLICT(project_key,recipient,message_id) DO UPDATE SET "
            "state=CASE WHEN receipts.state='processed' THEN receipts.state ELSE 'stale' END,"
            "reason=CASE WHEN receipts.state='processed' THEN receipts.reason ELSE excluded.reason END,"
            "ack_pending=1,updated_ts=excluded.updated_ts",
            (project, recipient, int(message_id), reason, current, current),
        )


def complete_message(
    project_key: str, recipient: str, message_id: int, *,
    claim_token: str | None = None, now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM receipts WHERE project_key=? AND recipient=? AND message_id=?",
            (project, recipient, int(message_id)),
        ).fetchone()
        if not row:
            raise ValueError(f"消息 #{message_id} 尚未 claim")
        if row["state"] == "stale":
            con.commit()
            return {"completed": False, "stale": True, **dict(row)}
        if row["state"] == "processed":
            con.commit()
            return {
                "completed": True, "already_processed": True,
                "needs_resume": False, **dict(row),
            }
        if row["state"] != "claimed":
            raise ValueError(f"消息 #{message_id} 当前不可完成: {row['state']}")
        if (
            row["claim_expires_ts"] is not None
            and float(row["claim_expires_ts"]) <= current
        ):
            raise ValueError(f"消息 #{message_id} 的 claim 已过期")
        if row["claim_token"] and claim_token != row["claim_token"]:
            raise ValueError(f"消息 #{message_id} 的 claim 已失效")
        con.execute(
            "UPDATE receipts SET state='processed',ack_pending=1,claim_owner=NULL,"
            "claim_token=NULL,claim_expires_ts=NULL,updated_ts=? "
            "WHERE project_key=? AND recipient=? "
            "AND message_id=?",
            (current, project, recipient, int(message_id)),
        )
        needs_resume = row["intent"] in INTERRUPT_INTENTS and row["intent"] not in NO_RESUME_INTENTS
        participant_state = (
            "stopped" if row["intent"] == "stop"
            else "redirected" if row["intent"] == "redirect"
            else "resume_pending" if needs_resume else "working"
        )
        if row["run_id"] and row["task_id"]:
            con.execute(
                "UPDATE participants SET state=?,updated_ts=? WHERE run_id=? "
                "AND participant_id=?",
                (participant_state, current, row["run_id"], row["task_id"]),
            )
        updated = con.execute(
            "SELECT * FROM receipts WHERE project_key=? AND recipient=? AND message_id=?",
            (project, recipient, int(message_id)),
        ).fetchone()
        con.commit()
        return {"completed": True, "needs_resume": needs_resume, **dict(updated)}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def resume_message(
    project_key: str, recipient: str, message_id: int, *, now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    con = _connect()
    try:
        # resume 的状态判定与 participants 写入放入同一 BEGIN IMMEDIATE 事务，
        # 避免 SELECT 后状态被并发改变（如新一轮 claim/complete）导致基于过期数据恢复。
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM receipts WHERE project_key=? AND recipient=? AND message_id=?",
            (project, recipient, int(message_id)),
        ).fetchone()
        if not row or row["state"] != "processed":
            con.commit()
            return {"resumed": False, "reason": "message_not_processed"}
        if row["intent"] in NO_RESUME_INTENTS:
            con.commit()
            return {"resumed": False, "reason": row["intent"]}
        context = con.execute(
            "SELECT r.state AS run_state,p.task_revision,p.state FROM runs r "
            "JOIN participants p ON p.run_id=r.run_id WHERE r.run_id=? "
            "AND p.participant_id=?",
            (row["run_id"], row["task_id"]),
        ).fetchone()
        if not context or context["run_state"] != "active":
            con.commit()
            return {"resumed": False, "reason": "run_not_active"}
        if int(context["task_revision"]) != int(row["task_revision"]):
            con.commit()
            return {"resumed": False, "reason": "revision_changed"}
        try:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
        except ValueError:
            checkpoint = {}
        if (
            checkpoint.get("step_state") == "uncertain"
            or checkpoint.get("in_flight_safe") is False
        ):
            con.commit()
            return {
                "resumed": False, "reason": "uncertain_checkpoint",
                "checkpoint": checkpoint,
            }
        con.execute(
            "UPDATE participants SET state='working',updated_ts=? WHERE run_id=? "
            "AND participant_id=?",
            (current, row["run_id"], row["task_id"]),
        )
        con.commit()
        return {"resumed": True, "checkpoint": checkpoint}
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def fail_message(
    project_key: str, recipient: str, message_id: int, reason: str,
    *, claim_token: str | None = None, now: float | None = None,
) -> bool:
    current = time.time() if now is None else now
    project = str(Path(project_key).expanduser().resolve())
    con = _connect()
    try:
        # lease 校验与 failed 写入置于同一 BEGIN IMMEDIATE 事务，阻止旧
        # claim_token 在过期→reclaim 后仍能把新持有者的消息标失败。
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT run_id,task_id,state,claim_token,claim_expires_ts FROM receipts "
            "WHERE project_key=? AND recipient=? "
            "AND message_id=?",
            (project, recipient, int(message_id)),
        ).fetchone()
        if not row:
            con.commit()
            return False
        if row["state"] != "claimed":
            con.commit()
            return False
        if (
            row["claim_expires_ts"] is not None
            and float(row["claim_expires_ts"]) <= current
        ):
            raise ValueError(f"消息 #{message_id} 的 claim 已过期")
        if row["claim_token"] and claim_token != row["claim_token"]:
            raise ValueError(f"消息 #{message_id} 的 claim 已失效")
        con.execute(
            "UPDATE receipts SET state='failed',reason=?,claim_owner=NULL,"
            "claim_token=NULL,claim_expires_ts=NULL,updated_ts=? "
            "WHERE project_key=? AND recipient=? "
            "AND message_id=?",
            (reason, current, project, recipient, int(message_id)),
        )
        if row["run_id"] and row["task_id"]:
            con.execute(
                "UPDATE participants SET state='working',updated_ts=? WHERE run_id=? "
                "AND participant_id=?",
                (current, row["run_id"], row["task_id"]),
            )
        con.commit()
        return True
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def mark_acked(project_key: str, recipient: str, message_id: int) -> None:
    project = str(Path(project_key).expanduser().resolve())
    with _connect() as con:
        con.execute(
            "UPDATE receipts SET ack_pending=0 WHERE project_key=? AND recipient=? "
            "AND message_id=?",
            (project, recipient, int(message_id)),
        )


def receipt(project_key: str, recipient: str, message_id: int) -> dict[str, Any] | None:
    project = str(Path(project_key).expanduser().resolve())
    with _connect() as con:
        return _dict(con.execute(
            "SELECT * FROM receipts WHERE project_key=? AND recipient=? AND message_id=?",
            (project, recipient, int(message_id)),
        ).fetchone())


def maintain_live_claims(snapshot: dict[str, Any], *, now: float | None = None) -> int:
    current = time.time() if now is None else now
    live = {
        (str(pane.get("session") or ""), str(pane.get("pane_id") or ""))
        for pane in snapshot.get("panes", [])
        if pane.get("pane_id") and pane.get("agent_status") not in ("done", "unknown")
    }
    renewed = 0
    with _connect() as con:
        rows = con.execute(
            "SELECT q.project_key,q.recipient,q.message_id,r.session,p.pane_id "
            "FROM receipts q JOIN runs r ON r.run_id=q.run_id "
            "JOIN participants p ON p.run_id=q.run_id AND p.participant_id=q.task_id "
            "WHERE q.state='claimed'"
        ).fetchall()
        for row in rows:
            if (row["session"], row["pane_id"]) in live:
                con.execute(
                    "UPDATE receipts SET claim_expires_ts=?,updated_ts=? "
                    "WHERE project_key=? AND recipient=? AND message_id=?",
                    (
                        current + CLAIM_TTL, current, row["project_key"],
                        row["recipient"], row["message_id"],
                    ),
                )
                renewed += 1
        expired = con.execute(
            "SELECT run_id,task_id FROM receipts WHERE state='claimed' "
            "AND claim_expires_ts<=?",
            (current,),
        ).fetchall()
        con.execute(
            "UPDATE receipts SET state='pending',reason='claim_expired',claim_owner=NULL,"
            "claim_token=NULL,claim_expires_ts=NULL,updated_ts=? "
            "WHERE state='claimed' AND claim_expires_ts<=?",
            (current, current),
        )
        for row in expired:
            con.execute(
                "UPDATE participants SET state='working',updated_ts=? WHERE run_id=? "
                "AND participant_id=? AND state='handling_interrupt'",
                (current, row["run_id"], row["task_id"]),
            )
    return renewed


def close_session(
    session: str, state: str = "completed", *, now: float | None = None,
) -> int:
    current = time.time() if now is None else now
    with _connect() as con:
        rows = con.execute(
            "SELECT run_id FROM runs WHERE session=? AND state='active'", (session,)
        ).fetchall()
        for row in rows:
            con.execute(
                "UPDATE runs SET state=?,closed_ts=? WHERE run_id=?",
                (state, current, row["run_id"]),
            )
            con.execute(
                "UPDATE participants SET state=?,updated_ts=? WHERE run_id=?",
                (state, current, row["run_id"]),
            )
        return len(rows)


def run_by_session(session: str) -> dict[str, Any] | None:
    with _connect() as con:
        run = _dict(con.execute(
            "SELECT * FROM runs WHERE session=? ORDER BY revision DESC LIMIT 1",
            (session,),
        ).fetchone())
    if run:
        run["participants"] = run_participants(str(run["run_id"]))
    return run


def _task_report_result(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _dict(row)
    if result:
        result["pending"] = bool(
            result.get("request_id")
            and result.get("request_id") != result.get("report_request_id")
            and not result.get("request_error")
        )
    return result


def task_report(session: str, pane_id: str) -> dict[str, Any] | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM task_reports WHERE session=? AND pane_id=?",
            (session, pane_id),
        ).fetchone()
    return _task_report_result(row)


def task_reports(session: str) -> dict[str, dict[str, Any]]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM task_reports WHERE session=?", (session,),
        ).fetchall()
    return {
        str(row["pane_id"]): result
        for row in rows if (result := _task_report_result(row)) is not None
    }


def request_task_report(
    session: str, pane_id: str, agent_type: str, mail_name: str | None,
    *, now: float | None = None, request_id: str | None = None,
) -> dict[str, Any]:
    """登记最新上报请求；同一 pane 换 Agent 时清掉旧 Agent 的报告。"""
    current = time.time() if now is None else now
    request_id = request_id or uuid.uuid4().hex
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        previous = con.execute(
            "SELECT agent_type,mail_name FROM task_reports "
            "WHERE session=? AND pane_id=?",
            (session, pane_id),
        ).fetchone()
        identity_changed = bool(previous) and (
            str(previous["agent_type"]) != str(agent_type)
            or (previous["mail_name"] or None) != (mail_name or None)
        )
        if previous:
            con.execute(
                "UPDATE task_reports SET agent_type=?,mail_name=?,request_id=?,"
                "requested_ts=?,request_error=NULL WHERE session=? AND pane_id=?",
                (agent_type, mail_name, request_id, current, session, pane_id),
            )
            if identity_changed:
                con.execute(
                    "UPDATE task_reports SET report_request_id=NULL,progress=NULL,"
                    "summary=NULL,next_step=NULL,blocker=NULL,reported_ts=NULL "
                    "WHERE session=? AND pane_id=?",
                    (session, pane_id),
                )
        else:
            con.execute(
                "INSERT INTO task_reports(session,pane_id,agent_type,mail_name,"
                "request_id,requested_ts) VALUES(?,?,?,?,?,?)",
                (session, pane_id, agent_type, mail_name, request_id, current),
            )
        row = con.execute(
            "SELECT * FROM task_reports WHERE session=? AND pane_id=?",
            (session, pane_id),
        ).fetchone()
    result = _task_report_result(row)
    assert result is not None
    return result


def fail_task_report_request(
    session: str, pane_id: str, request_id: str, error: str,
) -> bool:
    with _connect() as con:
        changed = con.execute(
            "UPDATE task_reports SET request_error=? WHERE session=? AND pane_id=? "
            "AND request_id=?",
            (str(error)[:TASK_REPORT_TEXT_LIMIT], session, pane_id, request_id),
        ).rowcount
    return bool(changed)


def _task_report_text(value: Any, field: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} 不能为空")
    if len(text) > TASK_REPORT_TEXT_LIMIT:
        raise ValueError(f"{field} 最长 {TASK_REPORT_TEXT_LIMIT} 字符")
    return text


def submit_task_report(
    session: str, pane_id: str, request_id: str, progress: int,
    summary: str, next_step: str = "", blocker: str = "",
    *, now: float | None = None,
) -> dict[str, Any]:
    """只接受当前 request_id，防止排队中的旧请求覆盖新进度。"""
    try:
        progress = int(progress)
    except (TypeError, ValueError) as exc:
        raise ValueError("progress 必须是 0-100 的整数") from exc
    if not 0 <= progress <= 100:
        raise ValueError("progress 必须在 0-100 之间")
    summary = _task_report_text(summary, "summary", required=True)
    next_step = _task_report_text(next_step, "next_step")
    blocker = _task_report_text(blocker, "blocker")
    current = time.time() if now is None else now
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT request_id FROM task_reports WHERE session=? AND pane_id=?",
            (session, pane_id),
        ).fetchone()
        if not row:
            raise ValueError("没有待处理的任务上报请求")
        if str(row["request_id"]) != str(request_id):
            raise ValueError("上报请求已过期，请处理最新请求")
        con.execute(
            "UPDATE task_reports SET report_request_id=?,progress=?,summary=?,"
            "next_step=?,blocker=?,reported_ts=?,request_error=NULL "
            "WHERE session=? AND pane_id=?",
            (
                request_id, progress, summary, next_step, blocker, current,
                session, pane_id,
            ),
        )
        updated = con.execute(
            "SELECT * FROM task_reports WHERE session=? AND pane_id=?",
            (session, pane_id),
        ).fetchone()
    result = _task_report_result(updated)
    assert result is not None
    return result


def enrich_message(project_key: str, message: dict[str, Any]) -> dict[str, Any]:
    result = dict(message)
    clean, meta = parse_metadata(str(result.get("body_md") or ""))
    result["body_md"] = clean
    result["coordination"] = {"meta": meta, "receipts": {}}
    for target in result.get("recipients") or []:
        name = target.get("name")
        if name:
            found = receipt(project_key, str(name), int(result["id"]))
            if found:
                result["coordination"]["receipts"][name] = found
    return result
