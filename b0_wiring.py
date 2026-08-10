"""b0_wiring.py — B0 产品接线（W1/W3/W4 + A2 凭证解析 + fanout/rebuild）。

按已接受 ADR（2026-08-09 Leader Mail §2/§2a 修订、2026-08-10 B0 门禁合同
§6/§7）实现：

- A2 凭证合同：registry_selector → 本地 Agent Mail registry 的
  registration_token → active+previous 双身份对本地 Hub dual-pull；
  message_id 去重后 observe/ingest；selector 不可解析/权限过宽/symlink/
  Hub mismatch/Hub 不可读 → CredentialUnavailable（credential_unavailable
  degraded），禁止双 active、禁止静默成功；token 只在内存，不落日志/DB。
- 消息 I/O 只落在 _poll_message_state 链路（本模块 poll_once）；
  _poll_live_state 只允许经 set_target_status 注入状态，禁止消息 I/O。
- 9 个 stable reason 冻结码（不得增删改名）。
- W1 HerdrPromptAdapter：prompt 被 Herdr 接受才算 delivered。
- W5 issuer-scoped control-event fanout（PREP 查询/ack 已隔离）。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import coordination
import herdr_client
import hub_client
import leader_binding
from deferred_delivery import DeferredDeliveryCore, DeferredEvent

REGISTRY_DIR = Path(
    os.environ.get(
        "AGENT_MAIL_REGISTRY_DIR", str(Path.home() / ".agent-mail" / "registry")
    )
).expanduser()

# 冻结：9 个 stable reason（ADR 门禁合同 §7，不得增删改名）
REASON_DEFERRED_WORKING = "deferred_working"
REASON_DEFERRED_DELIVERED = "deferred_delivered"
REASON_STALE_BINDING_VERSION = "stale_binding_version"
REASON_DUPLICATE_EVENT_ID = "duplicate_event_id"
REASON_BINDING_UPDATED = "binding_updated"
REASON_BINDING_CHANGED = "binding_changed"
REASON_CREDENTIAL_UNAVAILABLE = "credential_unavailable"
REASON_CROSS_RUN_FAIL_FAST = "cross_run_fail_fast"
REASON_RESTART_REBUILD = "restart_rebuild"
STABLE_REASONS = frozenset({
    REASON_DEFERRED_WORKING, REASON_DEFERRED_DELIVERED,
    REASON_STALE_BINDING_VERSION, REASON_DUPLICATE_EVENT_ID,
    REASON_BINDING_UPDATED, REASON_BINDING_CHANGED,
    REASON_CREDENTIAL_UNAVAILABLE, REASON_CROSS_RUN_FAIL_FAST,
    REASON_RESTART_REBUILD,
})

FLUSH_INTERVAL_S = 4.0


class CredentialUnavailable(RuntimeError):
    """A2 fail-closed：selector 不可解析/Hub 不可读（credential_unavailable）。"""


class CrossRunFailFast(RuntimeError):
    """跨 run 业务消息在发送端失败（stable reason: cross_run_fail_fast）。"""


def _check_ancestor_dirs(path: Path) -> None:
    """祖先目录门：registry 根到 selector 父目录不得是 symlink 或组/他人可写。"""
    parts: list[Path] = []
    cur = path.parent
    root = REGISTRY_DIR.resolve()
    while True:
        parts.append(cur)
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent
    for d in parts:
        if d.is_symlink():
            raise CredentialUnavailable(f"祖先目录是 symlink: {d}")
        try:
            st = d.stat()
        except OSError:
            raise CredentialUnavailable(f"祖先目录不可读: {d}")
        mode = stat.S_IMODE(st.st_mode)
        # 属主必须是当前用户，且除属主外无任何权限（0700）
        if st.st_uid != os.getuid():
            raise CredentialUnavailable(f"祖先目录属主非当前用户: {d}")
        if mode != 0o700:
            raise CredentialUnavailable(
                f"祖先目录权限必须是 0700，当前 {oct(mode)}: {d}"
            )


def scope_key(issuer: str, scope_kind: str, scope_id: str) -> str:
    return f"{issuer}/{scope_kind}/{scope_id}"


def split_scope_key(key: str) -> tuple[str, str, str]:
    issuer, _, rest = key.partition("/")
    scope_kind, _, scope_id = rest.partition("/")
    return issuer, scope_kind, scope_id


# ---------------------------------------------------------------------------
# A2 selector 解析（fail-closed）
# ---------------------------------------------------------------------------

def resolve_selector(selector: str) -> dict[str, Any]:
    """解析 registry_selector → identity（含内存中的 registration_token）。

    selector 形如 "<project_slug>/<agent>--<instance>.json"。任一校验失败
    抛 CredentialUnavailable：文件缺失、symlink、权限过宽（组/他人可读）、
    JSON 损坏、缺 registration_token、identity hub 与本地 Hub 不一致。
    """
    if not isinstance(selector, str) or not selector.strip():
        raise CredentialUnavailable("selector 为空")
    rel = selector.strip()
    if rel.startswith("/") or ".." in Path(rel).parts:
        raise CredentialUnavailable(f"selector 必须相对 registry 根: {rel!r}")
    path = REGISTRY_DIR / rel
    _check_ancestor_dirs(path)
    if not path.is_file():
        raise CredentialUnavailable(f"selector 不存在: {rel}")
    if path.is_symlink():
        raise CredentialUnavailable(f"selector 是 symlink: {rel}")
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode & 0o077:
        raise CredentialUnavailable(f"selector 权限过宽 {oct(mode)}: {rel}")
    try:
        identity = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CredentialUnavailable(f"selector 不可读/损坏: {rel}: {type(exc).__name__}")
    if not isinstance(identity, dict) or not identity.get("registration_token"):
        raise CredentialUnavailable(f"selector 缺 registration_token: {rel}")
    hub_url = hub_client.HUB or ""
    ident_hub = str(identity.get("hub") or "").rstrip("/")
    if hub_url and ident_hub and ident_hub != hub_url.rstrip("/"):
        raise CredentialUnavailable(f"identity hub mismatch: {rel}")
    return identity


def _hub_token() -> str:
    token = getattr(hub_client, "TOKEN", "") or ""
    if not token:
        raise CredentialUnavailable("本地 Hub client token 不可用")
    return token


def fetch_inbox_for(
    identity: dict[str, Any], *, unread_only: bool = False, limit: int = 50,
    include_bodies: bool = True,
) -> list[dict[str, Any]]:
    """以 identity 的 registration_token 对本地 Hub 真实 fetch_inbox。

    Bearer 为本地 Hub client token；registration_token 只作为工具参数在
    内存传递（Hub 侧强制身份隔离）。任何失败 → CredentialUnavailable。
    """
    hub_url = (hub_client.HUB or "").rstrip("/")
    if not hub_url:
        raise CredentialUnavailable("本地 Hub 未配置")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "fetch_inbox", "arguments": {
            "project_key": identity.get("project_key"),
            "agent_name": identity.get("name"),
            "registration_token": identity.get("registration_token"),
            "limit": int(limit),
            "unread_only": unread_only,
            "include_bodies": include_bodies,
        }},
    }
    req = urllib.request.Request(
        f"{hub_url}/api/", data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_hub_token()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise CredentialUnavailable(f"Hub HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CredentialUnavailable(f"Hub 不可读: {type(exc).__name__}")
    if out.get("error"):
        raise CredentialUnavailable("Hub rpc error")
    result = out.get("result") or {}
    if result.get("isError"):
        raise CredentialUnavailable("fetch_inbox rejected")
    text = "".join(
        c.get("text", "") for c in (result.get("content") or [])
        if isinstance(c, dict) and c.get("type") == "text"
    )
    try:
        data = json.loads(text)
    except ValueError:
        raise CredentialUnavailable("fetch_inbox 响应非 JSON")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        msgs = data.get("messages")
        if isinstance(msgs, list):
            return msgs
    raise CredentialUnavailable("fetch_inbox 响应结构异常")


# ---------------------------------------------------------------------------
# cross_run_fail_fast（发送端，stable reason 之一）
# ---------------------------------------------------------------------------

def cross_run_fail_fast(
    project_key: str, sender: str, recipients: list[str], *,
    sender_run_id: str | None,
    active_context: Callable[[str, str], dict[str, Any] | None] | None = None,
    run_independent: bool = False,
) -> dict[str, Any]:
    """普通业务消息的跨 run 发送端 fail-fast（不产生半成功）。

    默认：任一收件人不在发送者同一 active run → 发送前抛
    CrossRunFailFast；显式 run_independent=True（如 binding 事件）放行。
    """
    if run_independent:
        return {"failed": False, "reason": None, "run_independent": True}
    ctx_fn = active_context or coordination.active_context
    bad: list[str] = []
    for recipient in recipients:
        ctx = ctx_fn(project_key, recipient)
        if not ctx or ctx.get("run_id") != sender_run_id:
            bad.append(recipient)
    if bad:
        raise CrossRunFailFast(
            f"cross_run_fail_fast: 收件人不在发送者 active run: {bad}"
        )
    return {"failed": False, "reason": None, "run_independent": False}


# ---------------------------------------------------------------------------
# 投递权威：复用 coordination receipts（R3：不新建第二 outbox）
# ---------------------------------------------------------------------------

def mark_prompted(project_key: str, mail_name: str, message_id: int) -> bool:
    """在权威 receipts 上写 b0_prompted 标记（单事务原子 upsert）。

    可证明失败语义：标记写入成功才允许 prompt；写失败则不 prompt、事件
    保留 pending 重试。重启后凡带标记的消息不再重复 prompt。
    无 receipt 行时插入 state='notified'：不在 claim 阻塞集
    （processed/stale/claimed）内，agent 后续 claim 正常进行且 checkpoint_json
    标记被保留。
    """
    con = coordination._connect()
    try:
        now = time.time()
        data = {"b0_prompted": now}
        row = con.execute(
            "SELECT checkpoint_json FROM receipts "
            "WHERE project_key=? AND recipient=? AND message_id=?",
            (project_key, mail_name, message_id),
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO receipts(project_key,recipient,message_id,intent,"
                "importance,state,reason,checkpoint_json,ack_pending,"
                "created_ts,updated_ts) "
                "VALUES(?,?,?,'info','normal','notified','b0_prompted',?,1,?,?)",
                (project_key, mail_name, message_id,
                 json.dumps(data, ensure_ascii=False), now, now),
            )
        else:
            try:
                merged = json.loads(row["checkpoint_json"] or "{}")
            except ValueError:
                merged = {}
            merged.update(data)
            con.execute(
                "UPDATE receipts SET checkpoint_json=?, updated_ts=? "
                "WHERE project_key=? AND recipient=? AND message_id=?",
                (json.dumps(merged, ensure_ascii=False), now,
                 project_key, mail_name, message_id),
            )
        con.commit()
        return True
    finally:
        con.close()


def is_prompted(project_key: str, mail_name: str, message_id: int) -> bool:
    receipt = coordination.receipt(project_key, mail_name, message_id)
    if receipt is None:
        return False
    return "b0_prompted" in str(receipt.get("checkpoint_json") or "")


def receipt_drain_counters(
    project_key: str, mail_name: str,
) -> dict[str, int]:
    """真实排空计数（权威 receipts）：pending=未 processed 的行，
    claimed=认领中，ack_pending=未回执。drain 只认全零。"""
    con = coordination._connect()
    try:
        where = "WHERE project_key=? AND recipient=?"
        params = (project_key, mail_name)
        pending = int(con.execute(
            f"SELECT COUNT(*) FROM receipts {where} AND state!='processed'",
            params,
        ).fetchone()[0])
        claimed = int(con.execute(
            f"SELECT COUNT(*) FROM receipts {where} AND state='claimed'",
            params,
        ).fetchone()[0])
        ack_pending = int(con.execute(
            f"SELECT COALESCE(SUM(ack_pending),0) FROM receipts {where}",
            params,
        ).fetchone()[0])
        return {"pending": pending, "claimed": claimed, "ack_pending": ack_pending}
    finally:
        con.close()


class _ProofAdapter:
    """包装投递 adapter：1) mail 事件先写 receipts b0_prompted 标记再投递
    （写失败不投递、保留 pending，可证明失败语义）；2) 记录本进程已真实
    delivered 的 event_id（投递证明）。

    证明集仅内存：crash 即失，未证明事件靠 outbox/receipts 重放（零丢）；
    mail 事件的跨重启防重由 receipts b0_prompted 标记承担。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.proof: set[str] = set()

    def deliver(
        self, scope: str, binding_version: int, events: list[DeferredEvent],
    ) -> bool:
        for ev in events:
            if not ev.event_id.startswith("mail:"):
                continue
            summary = ev.summary or {}
            project_key = str(summary.get("project_key") or "")
            mail_name = str(summary.get("mail_name") or "")
            message_id = summary.get("message_id")
            if not project_key or not mail_name or message_id is None:
                return False
            try:
                if not mark_prompted(project_key, mail_name, int(message_id)):
                    return False
            except Exception:
                logging.getLogger("b0.wiring").exception("mark_prompted failed")
                return False
        accepted = bool(self._inner.deliver(scope, binding_version, events))
        if accepted:
            for ev in events:
                self.proof.add(ev.event_id)
        return accepted


# ---------------------------------------------------------------------------
# W1 HerdrPromptAdapter
# ---------------------------------------------------------------------------

class HerdrPromptAdapter:
    """DeferredDeliveryCore 的投递适配：prompt 被 Herdr 接受才算 delivered。

    target_provider(scope) -> (session, pane_id) 由接线层提供（来自 active
    binding 的路由载荷）。无 session/pane → 拒绝（保留 pending 重试）。
    """

    def __init__(
        self,
        target_provider: Callable[[str], tuple[str, str] | None],
        mail_recv_hint: str = "运行 mail-recv --unread 领取",
    ) -> None:
        self._target_provider = target_provider
        self._mail_recv_hint = mail_recv_hint

    def deliver(
        self, scope: str, binding_version: int, events: list[DeferredEvent],
    ) -> bool:
        target = self._target_provider(scope)
        if not target:
            return False
        session, pane_id = target
        if not session or not pane_id:
            return False
        lines = []
        for ev in events:
            summary = ev.summary or {}
            kind = str(summary.get("kind") or "event")
            title = str(summary.get("title") or ev.event_id)
            sender = str(summary.get("from") or "")
            prefix = f"来自 {sender} 的" if sender else ""
            lines.append(f"- [{kind}] {prefix}{title}".rstrip())
        body = "\n".join(lines)
        note = (
            f"[Agent Cockpit B0] 有 {len(events)} 条待处理事件"
            f"（binding v{binding_version}）：\n{body}\n{self._mail_recv_hint}"
        )
        try:
            sent = herdr_client.pane_send(session, pane_id, note, "prompt")
        except Exception:
            return False
        if not isinstance(sent, dict):
            return False
        return not sent.get("error") and sent.get("available", True) is not False


# ---------------------------------------------------------------------------
# B0 协调器（dual-pull / ingest / fanout / rebuild）
# ---------------------------------------------------------------------------

class B0Coordinator:
    """单 issuer 的 B0 消息面接线。线程安全由 DeferredDeliveryCore 与内部锁保证。

    poll_once 只允许在消息 poller（_poll_message_state）链路调用；
    set_target_status 是 live poller 唯一允许的入口（禁消息 I/O）。
    """

    def __init__(
        self, adapter: Any, issuer: str, *,
        flush_interval: float = FLUSH_INTERVAL_S,
    ) -> None:
        self._issuer = issuer
        self._proof_adapter = _ProofAdapter(adapter)
        self.core = DeferredDeliveryCore(self._proof_adapter)
        self._flush_interval = float(flush_interval)
        self._last_full_flush = 0.0
        # Hub 全局 message_id 进程内去重（不得带 mail_name：同一 Hub 消息
        # 在 active/previous 两身份可见时必须只投一次）
        self._seen: set[int] = set()
        # scope -> degraded reason（credential_unavailable 可见）
        self.degraded: dict[str, str] = {}
        # 最近一次 poll 的 reason 统计（可观测性，G7）
        self.last_reasons: dict[str, int] = {}
        self._lock_scope = threading.RLock()

    # -- 状态注入（live poller 唯一入口，禁消息 I/O） ----------------------
    def set_target_status(self, scope: str, status: str) -> dict[str, Any]:
        return self.core.set_target_status(scope, status)

    # -- binding 同步 -------------------------------------------------------
    def active_bindings(self) -> list[dict[str, Any]]:
        return leader_binding.list_bindings(issuer=self._issuer, state="active")

    def sync_bindings(self) -> list[dict[str, Any]]:
        """把 DB 中 active binding 同步进 core；cleared 事件 handoff 重投。"""
        results = []
        for row in self.active_bindings():
            key = scope_key(row["issuer"], row["scope_kind"], row["scope_id"])
            version = int(row["binding_version"])
            res = self.core.set_active_binding(key, version)
            # 版本切换的 cleared pending 不得静默丢弃：按新版本重新 ingest
            for event in res.get("cleared_pending_events") or []:
                self.core.ingest(replace(event, binding_version=version))
            res["scope"] = key
            results.append(res)
        return results

    def _record(self, reason: str) -> None:
        self.last_reasons[reason] = self.last_reasons.get(reason, 0) + 1

    # -- dual-pull ------------------------------------------------------------
    def _identities_for(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """active + previous（未排空）身份的 identity 列表（A2 dual-pull）。"""
        identities: list[dict[str, Any]] = []
        selector = row.get("registry_selector")
        if selector:
            identities.append({"selector": selector, "role": "active"})
        prev_name = row.get("previous_mail_name")
        if prev_name:
            prev_row = leader_binding.get_binding(
                row["issuer"], row["scope_kind"], row["scope_id"], prev_name,
            )
            if (
                prev_row is not None
                and (prev_row.get("previous_state") or "") in ("pending", "draining")
                and prev_row.get("registry_selector")
            ):
                identities.append({
                    "selector": prev_row["registry_selector"], "role": "previous",
                    "prev_row": prev_row,
                })
        return identities

    def poll_once(self, *, unread_only: bool = False) -> dict[str, Any]:
        """一次全量 dual-pull + observe + deferred ingest（消息 poller 调用）。"""
        self.last_reasons = {}
        now = time.monotonic()
        full_flush = unread_only or (now - self._last_full_flush) >= self._flush_interval
        if full_flush:
            self._last_full_flush = now
        stats = {"scopes": 0, "pulled": 0, "ingested": 0,
                 "duplicate": 0, "stale": 0, "degraded": 0, "skipped": 0}
        for row in self.active_bindings():
            key = scope_key(row["issuer"], row["scope_kind"], row["scope_id"])
            version = int(row["binding_version"])
            stats["scopes"] += 1
            scope_failures = 0
            identities = self._identities_for(row)
            for ident_meta in identities:
                try:
                    identity = resolve_selector(ident_meta["selector"])
                    messages = fetch_inbox_for(identity, unread_only=unread_only)
                except CredentialUnavailable as exc:
                    scope_failures += 1
                    self._record(REASON_CREDENTIAL_UNAVAILABLE)
                    stats["degraded"] += 1
                    _log_degraded(key, ident_meta["role"], exc)
                    continue
                stats["pulled"] += len(messages)
                self._observe(identity, messages)
                for message in messages:
                    outcome = self._ingest_message(
                        key, version, identity, message,
                        skip_processed=full_flush,
                    )
                    stats[outcome] = stats.get(outcome, 0) + 1
                if ident_meta["role"] == "previous" and not messages:
                    self._close_drain(row, ident_meta.get("prev_row") or {}, identity)
            # degraded 按轮聚合：任一身份失败即保持 degraded（部分成功不清除）
            if scope_failures:
                self.degraded[key] = REASON_CREDENTIAL_UNAVAILABLE
            elif identities:
                self.degraded.pop(key, None)
        if full_flush:
            # 4s 全量兜底：显式 flush 所有 scope（不依赖状态翻转）
            for row in self.active_bindings():
                key = scope_key(row["issuer"], row["scope_kind"], row["scope_id"])
                try:
                    result = self.core.flush(key)
                except Exception:
                    logging.getLogger("b0.wiring").exception(
                        "b0 flush failed scope=%s", key,
                    )
                    continue
                if result.get("delivered"):
                    self._record(REASON_DEFERRED_DELIVERED)
        return stats

    def _observe(
        self, identity: dict[str, Any], messages: list[dict[str, Any]],
    ) -> None:
        if not messages:
            return
        project_key = str(identity.get("project_key") or "")
        recipient = str(identity.get("name") or "")
        if not project_key or not recipient:
            return
        try:
            coordination.observe_messages(project_key, recipient, messages)
        except Exception:
            # observe 失败不阻塞通知链；receipt 去重仍由 _seen/DB 承担
            pass

    def _ingest_message(
        self, scope: str, version: int, identity: dict[str, Any],
        message: dict[str, Any], *, skip_processed: bool,
    ) -> str:
        mail_name = str(identity.get("name") or "")
        try:
            message_id = int(message.get("id") or -1)
        except (TypeError, ValueError):
            message_id = -1
        if message_id < 0:
            return "skipped"
        # 去重键是 Hub 全局 message_id：同一 Hub 消息在 active/previous
        # 两身份可见时只允许一次 prompt
        if message_id in self._seen:
            self._record(REASON_DUPLICATE_EVENT_ID)
            return "duplicate"
        event_id = f"mail:{message_id}"
        # receipts 权威：已带 b0_prompted 标记的消息（跨重启）不得再次 prompt
        if is_prompted(
            str(identity.get("project_key") or ""), mail_name, message_id,
        ):
            self._seen.add(message_id)
            self._record(REASON_DUPLICATE_EVENT_ID)
            return "duplicate"
        if skip_processed:
            receipt = coordination.receipt(
                str(identity.get("project_key") or ""), mail_name, message_id,
            )
            if receipt is not None and receipt.get("state") in (
                "processed", "claimed",
            ):
                self._seen.add(message_id)
                self._record(REASON_DUPLICATE_EVENT_ID)
                return "duplicate"
        self._seen.add(message_id)
        event = DeferredEvent(
            event_id=event_id,
            scope=scope,
            binding_version=version,
            summary={
                "kind": str(message.get("kind") or "message"),
                "title": str(message.get("subject") or f"#{message_id}"),
                "from": str(message.get("from") or message.get("sender_id") or ""),
                "message_id": message_id,
                "mail_name": mail_name,
                "project_key": str(identity.get("project_key") or ""),
                "importance": message.get("importance"),
                "created_ts": message.get("created_ts"),
            },
            created_ts=float(message.get("created_ts") or time.time()),
        )
        result = self.core.ingest(event)
        if result.get("stale"):
            self._record(REASON_STALE_BINDING_VERSION)
            return "stale"
        if result.get("duplicate"):
            self._record(REASON_DUPLICATE_EVENT_ID)
            return "duplicate"
        if result.get("delivered"):
            self._record(REASON_DEFERRED_DELIVERED)
        else:
            self._record(REASON_DEFERRED_WORKING)
        return "ingested"

    def _close_drain(
        self, row: dict[str, Any], prev_row: dict[str, Any],
        identity: dict[str, Any],
    ) -> None:
        """previous 邮箱排空闭环：只认真实证明。Hub 拉空 = remaining 0；
        receipts 权威计数（pending/claimed/ack_pending）必须全零。任一非零
        保持 draining 等下轮；PREP 强制 CAS，stale 零变更。"""
        if not prev_row:
            return
        project_key = str(identity.get("project_key") or "")
        prev_name = str(prev_row.get("mail_name") or "")
        if not project_key or not prev_name:
            return
        try:
            counters = receipt_drain_counters(project_key, prev_name)
        except Exception:
            logging.getLogger("b0.wiring").exception("drain counters failed")
            return
        if any(counters.values()):
            return  # 未排空：真实计数非零，禁止伪造 drained
        try:
            leader_binding.mark_previous_state(
                row["issuer"], row["scope_kind"], row["scope_id"],
                prev_name,
                state="drained",
                expected_binding_version=int(prev_row.get("binding_version") or 0),
                expected_migration_id=str(prev_row.get("migration_id") or ""),
                expected_state=str(prev_row.get("previous_state") or "pending"),
                expected_drain_revision=int(prev_row.get("drain_revision") or 0),
                remaining=0,
                pending=counters["pending"],
                claimed=counters["claimed"],
                ack_pending=counters["ack_pending"],
                reason="hub_inbox_empty+receipts_zero",
            )
        except leader_binding.StaleVersionError:
            pass  # 并发/旧轮 worker：零变更即正确
        except Exception:
            logging.getLogger("b0.wiring").exception("close drain failed")

    # -- W5 control-event fanout（issuer-scoped） ---------------------------
    def fanout_control_events(self, *, limit: int = 100) -> int:
        """把本 issuer 未 fanout 的 control events 注入 deferred。

        只有事件真正 delivered 才 mark fanned_out；working/拒绝时保持未
        mark，下一轮 tick 或 crash 重建后重放（零丢零重由 event_id 去重）。
        跨 issuer 读/ack 由 PREP 层强制零变更（WHERE issuer 隔离）。
        """
        fanned = 0
        for event in leader_binding.undelivered_control_events(
            self._issuer, limit=limit,
        ):
            key = scope_key(
                event["issuer"], event["scope_kind"], event["scope_id"],
            )
            event_type = str(event.get("event_type") or "")
            if event_type in ("binding_changed", "binding_updated"):
                self._record(
                    REASON_BINDING_CHANGED if event_type == "binding_changed"
                    else REASON_BINDING_UPDATED
                )
            payload: dict[str, Any] = {}
            try:
                payload = json.loads(event.get("payload_json") or "{}")
            except ValueError:
                payload = {}
            result = self.core.ingest(DeferredEvent(
                event_id=str(event["event_id"]),
                scope=key,
                binding_version=int(event["binding_version"]),
                summary={
                    "kind": "control_event",
                    "title": f"{event_type} v{event['binding_version']}",
                    "event_type": event_type,
                    "mail_name": payload.get("mail_name"),
                    "previous_mail_name": payload.get("previous_mail_name"),
                },
                created_ts=float(event.get("created_ts") or time.time()),
            ))
            if result.get("delivered") or (
                str(event["event_id"]) in self._proof_adapter.proof
            ):
                # 只有 durable 投递证明（本进程 adapter 真实 accept）才 ack；
                # pending/仅 duplicate 不得 mark，crash 后由 outbox 重放
                if leader_binding.mark_event_fanned_out(
                    self._issuer, str(event["event_id"]),
                ):
                    fanned += 1
        return fanned

    # -- restart rebuild ------------------------------------------------------
    def rebuild(self) -> dict[str, Any]:
        """重启重建：同步 active binding（幂等同版本）+ 从 Hub unread 重建
        pending（receipt/seen 去重防重复 prompt）。"""
        sync = self.sync_bindings()
        stats = self.poll_once(unread_only=True)
        self._record(REASON_RESTART_REBUILD)
        return {"sync": sync, "poll": stats}

    # -- 可观测性（G7） -------------------------------------------------------
    def state(self) -> dict[str, Any]:
        scopes: dict[str, Any] = {}
        for row in self.active_bindings():
            key = scope_key(row["issuer"], row["scope_kind"], row["scope_id"])
            st = self.core.state(key)
            st["degraded"] = self.degraded.get(key)
            scopes[key] = st
        return {
            "issuer": self._issuer,
            "scopes": scopes,
            "last_reasons": dict(self.last_reasons),
        }


def _log_degraded(scope: str, role: str, exc: CredentialUnavailable) -> None:
    # 不落 token；只记录 scope/role 与失败类别
    logging.getLogger("b0.wiring").warning(
        "credential_unavailable scope=%s role=%s detail=%s",
        scope, role, str(exc)[:120],
    )


# ---------------------------------------------------------------------------
# B1 rebind 控制面（供 server.py 端点调用）
# ---------------------------------------------------------------------------

def rebind_authorize(
    *, user_authenticated: bool, caller_mail_name: str | None,
    active: dict[str, Any] | None,
    capability_digest: str | None = None,
    expected_digest: str | None = None,
) -> tuple[bool, str]:
    """B1 鉴权：COCKPIT_TOKEN 用户（或本机回环用户会话）直接通过；
    否则要求 caller mail_name == 当前 active binding mail_name，且必须
    附带 capability_digest = sha256(registration_token) 的能力证明
    （token 本身不出现在请求/日志中）。"""
    if user_authenticated:
        return True, "user"
    if caller_mail_name and active is not None:
        if hmac.compare_digest(
            str(caller_mail_name), str(active.get("mail_name") or ""),
        ):
            if (
                capability_digest and expected_digest
                and hmac.compare_digest(
                    str(capability_digest), str(expected_digest),
                )
            ):
                return True, "active_leader"
    return False, "unauthorized"


def perform_rebind(
    scope_kind: str, scope_id: str, *, issuer: str,
    mail_name: str, expected_version: int | None,
    agent_name: str | None = None, agent_kind: str | None = None,
    session: str | None = None, pane_id: str | None = None,
    registry_selector: str | None = None,
) -> dict[str, Any]:
    """B1 CAS 改绑（expected_version 必填；失败零变更由 PREP 保证）。"""
    row = leader_binding.bind_leader(
        issuer, scope_kind, scope_id,
        mail_name=mail_name,
        agent_name=agent_name, agent_kind=agent_kind,
        session=session, pane_id=pane_id,
        registry_selector=registry_selector,
        expected_version=expected_version,
    )
    return row
