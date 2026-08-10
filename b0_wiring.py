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
import ipaddress
import json
import logging
import os
import sqlite3
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

# B0 trust-domain issuer（与 server.B0_ISSUER 同源）
B0_ISSUER = os.environ.get("B0_ISSUER", "local").strip() or "local"

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
ScopeFilter = Callable[[str, str], bool]


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
    # JSON 数组编码：对含 '/' 等任意字符的输入保持单射可逆
    return json.dumps([issuer, scope_kind, scope_id], ensure_ascii=False)


def split_scope_key(key: str) -> tuple[str, str, str]:
    try:
        parts = json.loads(key)
        if isinstance(parts, list) and len(parts) == 3:
            return str(parts[0]), str(parts[1]), str(parts[2])
    except ValueError:
        pass
    issuer, _, rest = key.partition("/")
    scope_kind, _, scope_id = rest.partition("/")
    return issuer, scope_kind, scope_id


def _safe_ts(value: Any) -> float:
    """created_ts 宽容解析：数值/数值字符串有效，其余回退当前时间，
    绝不抛异常毒化去重。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return time.time()
    return time.time()


def _require_local_hub(url: str) -> None:
    """网络前 fail-closed：Hub 必须是本机回环，否则拒绝（capability 不得
    离开本机）。"""
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost":
            return
        raise CredentialUnavailable(f"Hub 必须是本机回环地址: {url!r}")
    if not ip.is_loopback:
        raise CredentialUnavailable(f"Hub 必须是本机回环地址: {url!r}")


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
    # 网络前 fail-closed：Bearer/registration_token 绝不得离开本机
    _require_local_hub(hub_url)
    ident_hub = str(identity.get("hub") or "")
    if ident_hub:
        _require_local_hub(ident_hub)
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "fetch_inbox", "arguments": {
            "project_key": identity.get("project_key"),
            "agent_name": identity.get("name"),
            "registration_token": identity.get("registration_token"),
            "limit": int(limit),
            "unread_only": unread_only,
            "include_bodies": include_bodies,
            "format": "json",
        }},
    }
    req = urllib.request.Request(
        f"{hub_url}/api/", data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {_hub_token()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            out = json.loads(hub_client._response_data(raw))
    except urllib.error.HTTPError as exc:
        raise CredentialUnavailable(f"Hub HTTP {exc.code}")
    except (urllib.error.URLError, OSError, UnicodeError, ValueError) as exc:
        raise CredentialUnavailable(f"Hub 不可读: {type(exc).__name__}")
    if not isinstance(out, dict):
        raise CredentialUnavailable("Hub rpc 响应结构异常")
    if out.get("error"):
        raise CredentialUnavailable("Hub rpc error")
    result = out.get("result") or {}
    if not isinstance(result, dict):
        raise CredentialUnavailable("fetch_inbox 响应结构异常")
    if result.get("isError"):
        raise CredentialUnavailable("fetch_inbox rejected")
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        data = structured["result"]
    else:
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


def shadow_probe(
    issuer: str, *, scope_filter: ScopeFilter | None = None,
) -> dict[str, Any]:
    """只读验证 binding store、A2 selector 与 Hub 拉取链，不迁移或写库。"""
    path = leader_binding.DB_PATH
    if not path.is_file():
        return {
            "available": True, "degraded": False, "reason": None,
            "scopes": 0, "identities": 0, "pulled": 0,
        }
    try:
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
            return {
                "available": False, "degraded": True,
                "reason": "probe_requires_quiescence",
                "scopes": 0, "identities": 0, "pulled": 0,
            }
    except OSError:
        return {
            "available": False, "degraded": True,
            "reason": "binding_store_unavailable",
            "scopes": 0, "identities": 0, "pulled": 0,
        }
    required = {
        "issuer", "scope_kind", "scope_id", "mail_name", "state",
        "binding_version", "registry_selector", "previous_mail_name",
        "previous_state",
    }
    try:
        con = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=1.0,
        )
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        try:
            columns = {
                str(row[1]) for row in con.execute(
                    "PRAGMA table_info(leader_bindings)"
                ).fetchall()
            }
            if not required.issubset(columns):
                return {
                    "available": False, "degraded": True,
                    "reason": "binding_store_incompatible",
                    "scopes": 0, "identities": 0, "pulled": 0,
                }
            rows = [dict(row) for row in con.execute(
                "SELECT * FROM leader_bindings "
                "WHERE issuer=? AND state='active' "
                "ORDER BY scope_kind, scope_id",
                (issuer,),
            ).fetchall()]
            previous: dict[tuple[str, str, str], dict[str, Any]] = {}
            for row in con.execute(
                "SELECT * FROM leader_bindings "
                "WHERE issuer=? AND state='previous'",
                (issuer,),
            ).fetchall():
                item = dict(row)
                previous[(
                    str(item.get("scope_kind") or ""),
                    str(item.get("scope_id") or ""),
                    str(item.get("mail_name") or ""),
                )] = item
        finally:
            con.close()
    except sqlite3.Error:
        return {
            "available": False, "degraded": True,
            "reason": "binding_store_unavailable",
            "scopes": 0, "identities": 0, "pulled": 0,
        }

    allowed = scope_filter or (lambda _kind, _scope_id: True)
    checked_scopes = 0
    identities = 0
    pulled = 0
    failures = 0
    for row in rows:
        kind = str(row.get("scope_kind") or "")
        scope_id = str(row.get("scope_id") or "")
        if not allowed(kind, scope_id):
            continue
        checked_scopes += 1
        selectors: list[str] = []
        if row.get("registry_selector"):
            selectors.append(str(row["registry_selector"]))
        previous_name = str(row.get("previous_mail_name") or "")
        previous_row = previous.get((kind, scope_id, previous_name))
        if (
            previous_row
            and str(previous_row.get("previous_state") or "")
            in {"pending", "draining"}
            and previous_row.get("registry_selector")
        ):
            selectors.append(str(previous_row["registry_selector"]))
        if not selectors:
            failures += 1
            continue
        for selector in selectors:
            identities += 1
            try:
                identity = resolve_selector(selector)
                pulled += len(fetch_inbox_for(identity, unread_only=True))
            except CredentialUnavailable:
                failures += 1
    return {
        "available": failures == 0,
        "degraded": failures > 0,
        "reason": REASON_CREDENTIAL_UNAVAILABLE if failures else None,
        "scopes": checked_scopes,
        "identities": identities,
        "pulled": pulled,
    }


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
# F6/F7：binding control event 的 Hub transport（可 claim、携 binding version）
# ---------------------------------------------------------------------------

def active_run_participants() -> list[tuple[str, str]]:
    """coordination 权威：所有 active run 的参与者 (project_key, mail_name)。"""
    con = coordination._connect()
    try:
        rows = con.execute(
            "SELECT r.project_key AS project_key, p.mail_name AS mail_name "
            "FROM participants p JOIN runs r ON r.run_id = p.run_id "
            "WHERE r.state='active' AND p.mail_name IS NOT NULL "
            "AND p.mail_name != ''",
        ).fetchall()
    finally:
        con.close()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for row in rows:
        item = (str(row["project_key"]), str(row["mail_name"]))
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def send_control_message_to_participants(
    event: dict[str, Any], sender_identity: dict[str, Any],
) -> bool:
    """ADR 故障6/7：向各 active run 参与者发送可 claim 的 binding control
    message。正文携带 binding version 与 scope；不附 task/revision；
    ack_required=True。无参与者时空真（vacuous）成功。发送失败返回 False
    （fanout 保持未 mark，下轮重试）。"""
    participants = active_run_participants()
    if not participants:
        return True
    sender_name = str(sender_identity.get("name") or "")
    sender_token = str(sender_identity.get("registration_token") or "")
    if not sender_name or not sender_token:
        return False
    event_type = str(event.get("event_type") or "binding_event")
    version = int(event.get("binding_version") or 0)
    event_id = str(event.get("event_id") or "")
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(event.get("payload_json") or "{}")
    except ValueError:
        payload = {}
    body = (
        f"[binding {event_type} v{version}] "
        f"event_id={event_id} "
        f"scope={event.get('scope_kind')}/{event.get('scope_id')} "
        f"issuer={event.get('issuer')} "
        f"mail_name={payload.get('mail_name') or ''} "
        f"previous_mail_name={payload.get('previous_mail_name') or ''}\n"
        "此为团队/频道控制面事件，不附 task/revision；"
        f"请以 binding version v{version} 校验后 claim 处理；"
        f"event_id={event_id} 为稳定幂等键，重复送达按同一事件去重。"
    )
    subject = f"[binding {event_type}] v{version} #{event_id}"
    by_project: dict[str, list[str]] = {}
    for project_key, mail_name in participants:
        by_project.setdefault(project_key, []).append(mail_name)
    ok = True
    for project_key, names in sorted(by_project.items()):
        try:
            hub_client.send_message(
                project_key=project_key,
                sender_name=sender_name,
                sender_token=sender_token,
                to=sorted(set(names)),
                subject=subject,
                body_md=body,
                importance="normal",
                ack_required=True,
            )
        except Exception:
            logging.getLogger("b0.wiring").exception(
                "control message transport failed project=%s", project_key,
            )
            ok = False
    return ok


# ---------------------------------------------------------------------------
# 投递权威：复用 coordination receipts（R3：不新建第二 outbox）
# ---------------------------------------------------------------------------

def set_prompt_marker(
    project_key: str, mail_name: str, message_id: int, phase: str,
) -> bool:
    """在权威 receipts 上写投递标记（单事务原子 upsert）。

    两相语义（与真实 accept 闭合）：
    - phase='attempt'：投递尝试前写入；adapter 拒绝时必须 clear，重启后
      允许补投（不得永久漏）。
    - phase='delivered'：adapter 真实 accept 后写入，是唯一跨重启防重的
      durable 证明。
    无 receipt 行时插入 state='notified'（不在 claim 阻塞集内）。
    """
    if phase not in ("attempt", "delivered"):
        raise ValueError(f"非法 phase: {phase!r}")
    con = coordination._connect()
    try:
        now = time.time()
        key = f"b0_prompt_{phase}" if phase == "attempt" else "b0_prompted"
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
                 json.dumps({key: now}, ensure_ascii=False), now, now),
            )
        else:
            try:
                merged = json.loads(row["checkpoint_json"] or "{}")
            except ValueError:
                merged = {}
            merged[key] = now
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


def clear_prompt_marker(
    project_key: str, mail_name: str, message_id: int,
) -> None:
    """adapter 拒绝后清除 attempt 标记：该消息重启后仍可补投（零漏）。"""
    con = coordination._connect()
    try:
        row = con.execute(
            "SELECT checkpoint_json FROM receipts "
            "WHERE project_key=? AND recipient=? AND message_id=?",
            (project_key, mail_name, message_id),
        ).fetchone()
        if row is None:
            return
        try:
            merged = json.loads(row["checkpoint_json"] or "{}")
        except ValueError:
            merged = {}
        if "b0_prompt_attempt" not in merged:
            return
        merged.pop("b0_prompt_attempt", None)
        con.execute(
            "UPDATE receipts SET checkpoint_json=?, updated_ts=? "
            "WHERE project_key=? AND recipient=? AND message_id=?",
            (json.dumps(merged, ensure_ascii=False), time.time(),
             project_key, mail_name, message_id),
        )
        con.commit()
    finally:
        con.close()


def prompt_marker_state(
    project_key: str, mail_name: str, message_id: int,
) -> str | None:
    """标记三态：'delivered'=durable 投递证明；'attempt'=尝试过但未证明
    （外部 accept 后 mark 崩溃的不确定态，按已投处理防重复）；None=从未尝试。"""
    receipt = coordination.receipt(project_key, mail_name, message_id)
    if receipt is None:
        return None
    try:
        data = json.loads(receipt.get("checkpoint_json") or "{}")
    except ValueError:
        return None
    if "b0_prompted" in data:
        return "delivered"
    if "b0_prompt_attempt" in data:
        return "attempt"
    return None


def is_prompted(project_key: str, mail_name: str, message_id: int) -> bool:
    """delivered 与 attempt（不确定）都阻塞重启重 prompt；拒绝路径已清
    attempt，故不会误伤可补投消息。"""
    return prompt_marker_state(project_key, mail_name, message_id) in (
        "delivered", "attempt",
    )


def set_ctl_transport_mark(event_id: str) -> bool:
    """transport 成功后的 durable 幂等标记（重启不得重发）。复用 receipts
    权威行 /b0/control/<event_id>，不新建 outbox。"""
    con = coordination._connect()
    try:
        now = time.time()
        row = con.execute(
            "SELECT checkpoint_json FROM receipts "
            "WHERE project_key='/b0/control' AND recipient=? AND message_id=0",
            (event_id,),
        ).fetchone()
        data: dict[str, Any] = {}
        if row is not None:
            try:
                data = json.loads(row["checkpoint_json"] or "{}")
            except ValueError:
                data = {}
        data["b0_ctl_transport"] = now
        if row is None:
            con.execute(
                "INSERT INTO receipts(project_key,recipient,message_id,intent,"
                "importance,state,reason,checkpoint_json,ack_pending,"
                "created_ts,updated_ts) "
                "VALUES('/b0/control',?,0,'info','normal','notified',"
                "'b0_ctl_transport',?,0,?,?)",
                (event_id, json.dumps(data, ensure_ascii=False), now, now),
            )
        else:
            con.execute(
                "UPDATE receipts SET checkpoint_json=?, updated_ts=? "
                "WHERE project_key='/b0/control' AND recipient=? AND message_id=0",
                (json.dumps(data, ensure_ascii=False), now, event_id),
            )
        con.commit()
        return True
    finally:
        con.close()


def ctl_transport_done(event_id: str) -> bool:
    receipt = coordination.receipt("/b0/control", event_id, 0)
    if receipt is None:
        return False
    try:
        data = json.loads(receipt.get("checkpoint_json") or "{}")
    except ValueError:
        return False
    return "b0_ctl_transport" in data


def make_control_claim_gate(
    issuer: str, *, scope_filter: ScopeFilter | None = None,
    enforce_all: bool = True,
) -> Callable[[str, str, dict[str, Any], dict[str, Any] | None], tuple[bool, str | None]]:
    """构造按 issuer/scope/version/sender 精确绑定的控制消息认领门。"""
    allowed = scope_filter or (lambda _kind, _scope_id: True)

    def gate(
        project: str, recipient: str, message: dict[str, Any],
        meta: dict[str, Any] | None,
    ) -> tuple[bool, str | None]:
        del project, recipient
        data = meta or {}
        intent = str(data.get("intent") or "info")
        if intent not in coordination.NO_RESUME_INTENTS:
            return True, None
        sender = str(message.get("from") or message.get("sender_name") or "")
        meta_issuer = str(data.get("binding_issuer") or "")
        scope_kind = str(data.get("binding_scope_kind") or "")
        scope_id = str(data.get("binding_scope_id") or "")
        has_scope = bool(meta_issuer or scope_kind or scope_id)
        if has_scope:
            if (
                meta_issuer != issuer
                or scope_kind not in leader_binding.SCOPE_KINDS
                or not scope_id
            ):
                return False, REASON_STALE_BINDING_VERSION
            if not allowed(scope_kind, scope_id):
                return True, None
            try:
                active = leader_binding.get_active_binding(
                    issuer, scope_kind, scope_id,
                )
            except Exception:
                logging.getLogger("b0.wiring").exception(
                    "claim gate: scoped binding unreadable"
                )
                return False, REASON_STALE_BINDING_VERSION
            try:
                version = int(data.get("binding_version"))
            except (TypeError, ValueError):
                return False, REASON_STALE_BINDING_VERSION
            if (
                not active
                or version != int(active.get("binding_version") or -1)
                or not sender
                or not hmac.compare_digest(
                    sender, str(active.get("mail_name") or ""),
                )
            ):
                return False, REASON_STALE_BINDING_VERSION
            return True, None

        try:
            bindings = [
                row for row in leader_binding.list_bindings(
                    issuer=issuer, state="active",
                )
                if allowed(
                    str(row.get("scope_kind") or ""),
                    str(row.get("scope_id") or ""),
                )
            ]
        except Exception:
            logging.getLogger("b0.wiring").exception(
                "claim gate: bindings unreadable"
            )
            return (
                (False, REASON_STALE_BINDING_VERSION)
                if enforce_all else (True, None)
            )
        if not bindings:
            return True, None
        if any(
            sender and hmac.compare_digest(
                sender, str(row.get("mail_name") or ""),
            )
            for row in bindings
        ):
            return False, REASON_STALE_BINDING_VERSION
        return (
            (False, REASON_STALE_BINDING_VERSION)
            if enforce_all else (True, None)
        )

    return gate


def control_claim_gate(
    project: str, recipient: str, message: dict[str, Any],
    meta: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    return make_control_claim_gate(B0_ISSUER)(project, recipient, message, meta)


def install_claim_gate(
    *, issuer: str | None = None, scope_filter: ScopeFilter | None = None,
    enforce_all: bool = True,
) -> None:
    """把 scope-aware W6 认领门安装到 coordination.claim_message。"""
    coordination.CONTROL_CLAIM_GATE = make_control_claim_gate(
        issuer or B0_ISSUER,
        scope_filter=scope_filter,
        enforce_all=enforce_all,
    )


def uninstall_claim_gate() -> None:
    coordination.CONTROL_CLAIM_GATE = None


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
        # accept 成功但 delivered-mark 崩溃的事件：下轮只补标记、不重 prompt
        self._pending_marks: dict[str, list[tuple[str, str, int]]] = {}

    @staticmethod
    def _marker_targets(events: list[DeferredEvent]) -> list[tuple[str, str, int]] | None:
        """mail 事件 → (project_key, mail_name, message_id)；
        control 事件 → ("b0://control", event_id, 0)。缺字段返回 None。"""
        targets: list[tuple[str, str, int]] = []
        for ev in events:
            if ev.event_id.startswith("mail:"):
                summary = ev.summary or {}
                project_key = str(summary.get("project_key") or "")
                mail_name = str(summary.get("mail_name") or "")
                message_id = summary.get("message_id")
                if not project_key or not mail_name or message_id is None:
                    return None
                targets.append((project_key, mail_name, int(message_id)))
            else:
                targets.append(("/b0/control", ev.event_id, 0))
        return targets

    def deliver(
        self, scope: str, binding_version: int, events: list[DeferredEvent],
    ) -> bool:
        targets = self._marker_targets(events)
        if targets is None:
            return False
        event_ids = [ev.event_id for ev in events]
        # 恢复路径：上轮外部已 accept 但 delivered-mark 崩溃 → 只补标记
        if event_ids and all(eid in self._pending_marks for eid in event_ids):
            ok = True
            for eid in event_ids:
                for pk, mn, mid in self._pending_marks[eid]:
                    try:
                        if not set_prompt_marker(pk, mn, mid, "delivered"):
                            ok = False
                    except Exception:
                        ok = False
                if ok:
                    self._pending_marks.pop(eid, None)
                    self.proof.add(eid)
            return ok
        # 先写 attempt：写失败则不投递（保留 pending 重试）
        for pk, mn, mid in targets:
            try:
                if not set_prompt_marker(pk, mn, mid, "attempt"):
                    return False
            except Exception:
                logging.getLogger("b0.wiring").exception("set_prompt_marker failed")
                return False
        try:
            accepted = bool(self._inner.deliver(scope, binding_version, events))
        except Exception:
            accepted = False
        if not accepted:
            # 拒绝 → 清除 attempt：重启后允许补投（不得永久漏）
            for pk, mn, mid in targets:
                try:
                    clear_prompt_marker(pk, mn, mid)
                except Exception:
                    logging.getLogger("b0.wiring").exception(
                        "clear_prompt_marker failed",
                    )
            return False
        # 外部已 accept：升级 durable delivered 证明。写失败 = 不确定投递，
        # 不得声称 delivered（禁止 catch 掩盖）；记入待补标记，下轮幂等恢复
        mark_ok = True
        for pk, mn, mid in targets:
            try:
                if not set_prompt_marker(pk, mn, mid, "delivered"):
                    mark_ok = False
            except Exception:
                logging.getLogger("b0.wiring").exception(
                    "set_prompt_marker delivered failed",
                )
                mark_ok = False
        if not mark_ok:
            for eid in event_ids:
                self._pending_marks[eid] = targets
            return False
        for ev in events:
            self.proof.add(ev.event_id)
        return True


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
        scope_filter: ScopeFilter | None = None,
    ) -> None:
        self._issuer = issuer
        self._proof_adapter = _ProofAdapter(adapter)
        self.core = DeferredDeliveryCore(self._proof_adapter)
        self._flush_interval = float(flush_interval)
        self._scope_filter = scope_filter or (lambda _kind, _scope_id: True)
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
        return [
            row for row in leader_binding.list_bindings(
                issuer=self._issuer, state="active",
            )
            if self._scope_filter(
                str(row.get("scope_kind") or ""),
                str(row.get("scope_id") or ""),
            )
        ]

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
            created_ts=_safe_ts(message.get("created_ts")),
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
            drained = leader_binding.mark_previous_state(
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
            return  # 并发/旧轮 worker：零变更即正确
        except Exception:
            logging.getLogger("b0.wiring").exception("close drain failed")
            return
        if not drained.get("updated"):
            return
        # drained → retired 闭环（PREP CAS：state='drained'、revision+1）
        try:
            retired = leader_binding.retire_binding(
                row["issuer"], row["scope_kind"], row["scope_id"],
                prev_name,
                expected_binding_version=int(prev_row.get("binding_version") or 0),
                expected_migration_id=str(prev_row.get("migration_id") or ""),
                expected_state="drained",
                expected_drain_revision=int(prev_row.get("drain_revision") or 0) + 1,
                reason="b0_drain_closed",
            )
        except leader_binding.StaleVersionError:
            return
        except Exception:
            logging.getLogger("b0.wiring").exception("retire failed")
            return
        if not retired.get("retired"):
            return
        # A2：retire 后清除 previous selector（token 引用不再保留）
        try:
            con = leader_binding._connect()
            try:
                con.execute(
                    "UPDATE leader_bindings SET registry_selector=NULL, "
                    "updated_ts=? WHERE issuer=? AND scope_kind=? "
                    "AND scope_id=? AND mail_name=? AND state='retired'",
                    (time.time(), row["issuer"], row["scope_kind"],
                     row["scope_id"], prev_name),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            logging.getLogger("b0.wiring").exception(
                "clear retired selector failed",
            )

    # -- W5 control-event fanout（issuer-scoped） ---------------------------
    def fanout_control_events(
        self, *, limit: int = 100,
        transport: Callable[[dict[str, Any]], bool] | None = None,
    ) -> int:
        """把本 issuer 未 fanout 的 control events 注入 deferred 并 transport。

        只有同时满足 1) 本进程 durable 投递证明（adapter 真实 accept）与
        2) transport 成功（F6/F7 Hub control message 已发出）才 mark
        fanned_out；任一不满足保持未 mark，下一轮 tick 或 crash 重建后重放。
        跨 issuer 读/ack 由 PREP 层强制零变更（WHERE issuer 隔离）。
        """
        fanned = 0
        for event in leader_binding.undelivered_control_events(
            self._issuer, limit=limit,
        ):
            if not self._scope_filter(
                str(event.get("scope_kind") or ""),
                str(event.get("scope_id") or ""),
            ):
                continue
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
            event_id = str(event["event_id"])
            # 已投递（或不确定已投递）的事件不得重 prompt：跳过 ingest
            delivered_state = prompt_marker_state("/b0/control", event_id, 0)
            if delivered_state in ("delivered", "attempt"):
                delivered_proof = True
            else:
                result = self.core.ingest(DeferredEvent(
                    event_id=event_id,
                    scope=key,
                    binding_version=int(event["binding_version"]),
                    summary={
                        "kind": "control_event",
                        "title": f"{event_type} v{event['binding_version']}",
                        "event_type": event_type,
                        "mail_name": payload.get("mail_name"),
                        "previous_mail_name": payload.get("previous_mail_name"),
                    },
                    created_ts=_safe_ts(event.get("created_ts")),
                ))
                delivered_proof = (
                    result.get("delivered") or event_id in self._proof_adapter.proof
                )
            if not delivered_proof:
                continue  # pending/仅入队：不 mark，crash 可重放
            transported = True
            if transport is not None:
                # durable 幂等标记：transport 成功过则重启不重发
                if ctl_transport_done(event_id):
                    transported = True
                else:
                    try:
                        transported = bool(transport(event))
                    except Exception:
                        logging.getLogger("b0.wiring").exception(
                            "control transport failed",
                        )
                        transported = False
                    if transported:
                        try:
                            set_ctl_transport_mark(event_id)
                        except Exception:
                            logging.getLogger("b0.wiring").exception(
                                "set_ctl_transport_mark failed",
                            )
            if not transported:
                continue  # transport 失败不 mark，下轮重试
            try:
                marked = leader_binding.mark_event_fanned_out(
                    self._issuer, event_id,
                )
            except Exception:
                # mark 崩溃：不掩盖——事件保持 undelivered，投递/transport
                # 均有 durable 标记，下轮/重建后仅补 mark（零丢零重）
                logging.getLogger("b0.wiring").exception(
                    "mark_event_fanned_out failed; will retry",
                )
                continue
            if marked:
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
    """B1 CAS 改绑（expected_version 必填；失败零变更由 PREP 保证）。

    CAS 之前强制预校验（任一失败抛 BindingError，零变更）：
    - registry_selector 必填且可解析（A2 fail-closed 全套门）；
    - identity name 必须等于 mail_name（身份一致）；
    - identity hub 必须是本地 Hub（不得指向远端）；
    - session/pane_id 必填非空（路由完整性）。
    """
    if not registry_selector:
        raise leader_binding.BindingError("registry_selector 必填")
    try:
        identity = resolve_selector(registry_selector)
    except CredentialUnavailable as exc:
        raise leader_binding.BindingError(f"selector 校验失败: {exc}")
    if str(identity.get("name") or "") != mail_name:
        raise leader_binding.BindingError(
            f"selector 身份 {identity.get('name')!r} 与 mail_name {mail_name!r} 不符"
        )
    ident_hub = str(identity.get("hub") or "").rstrip("/")
    local_hub = (hub_client.HUB or "").rstrip("/")
    if not ident_hub or ident_hub != local_hub:
        raise leader_binding.BindingError("identity hub 必须是本地 Hub")
    if not str(session or "").strip() or not str(pane_id or "").strip():
        raise leader_binding.BindingError("session/pane_id 必填非空")
    row = leader_binding.bind_leader(
        issuer, scope_kind, scope_id,
        mail_name=mail_name,
        agent_name=agent_name, agent_kind=agent_kind,
        session=session, pane_id=pane_id,
        registry_selector=registry_selector,
        expected_version=expected_version,
    )
    return row
