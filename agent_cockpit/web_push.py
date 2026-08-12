"""Web Push subscription storage and VAPID delivery."""
from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import runtime_paths

ROOT = runtime_paths.INSTALL_ROOT
DATA_DIR = runtime_paths.data_root()
DB_PATH = runtime_paths.store("push")
KEY_PATH = runtime_paths.store("vapid")
VAPID_SUBJECT = os.environ.get(
    "COCKPIT_VAPID_SUBJECT", "mailto:agent-cockpit@localhost"
)
logger = logging.getLogger("agent-cockpit.web-push")


def _db() -> sqlite3.Connection:
    runtime_paths.validate_store("push")  # R3-B:symlink 逃逸 fail-closed
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE IF NOT EXISTS subscriptions ("
            "endpoint TEXT PRIMARY KEY, payload TEXT NOT NULL, created_ts REAL NOT NULL)"
        )
        return con
    except BaseException:
        con.close()
        raise


@contextmanager
def _managed_db():
    con = _db()
    try:
        with con:
            yield con
    finally:
        con.close()


def _validated(subscription: dict[str, Any]) -> dict[str, Any]:
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ValueError("Push endpoint 必须使用 HTTPS")
    if len(endpoint) > 4096:
        raise ValueError("Push endpoint 过长")
    if not isinstance(keys, dict) or not all(
        isinstance(keys.get(name), str) and 1 <= len(keys[name]) <= 1024
        for name in ("p256dh", "auth")
    ):
        raise ValueError("Push subscription keys 无效")
    return {
        "endpoint": endpoint,
        "expirationTime": subscription.get("expirationTime"),
        "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
    }


def save_subscription(subscription: dict[str, Any]) -> dict[str, bool]:
    value = _validated(subscription)
    with _managed_db() as con:
        con.execute(
            "INSERT INTO subscriptions(endpoint, payload, created_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET payload=excluded.payload",
            (value["endpoint"], json.dumps(value), time.time()),
        )
        con.commit()
    return {"ok": True}


def list_subscriptions() -> list[dict[str, Any]]:
    with _managed_db() as con:
        rows = con.execute("SELECT payload FROM subscriptions ORDER BY created_ts").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def delete_subscription(endpoint: str) -> bool:
    with _managed_db() as con:
        cursor = con.execute("DELETE FROM subscriptions WHERE endpoint = ?", (endpoint,))
        con.commit()
    return cursor.rowcount > 0


def _ensure_private_key() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    runtime_paths.validate_store("vapid")  # R3-B:symlink 逃逸 fail-closed
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        pem = KEY_PATH.read_bytes()
    except FileNotFoundError:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        try:
            fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pem = KEY_PATH.read_bytes()
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(pem)
                stream.flush()
                os.fsync(stream.fileno())
    key = serialization.load_pem_private_key(pem, password=None)
    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public_key = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")
    return str(KEY_PATH), public_key


def _public_key_from_private(value: str) -> str:
    """从 PEM 路径/PEM 正文/32-byte base64url 私钥推导 VAPID 公钥。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if "-----BEGIN" in value:
        private_key = serialization.load_pem_private_key(value.encode(), password=None)
    else:
        path = Path(value).expanduser()
        try:
            is_file = len(value) < 4096 and path.is_file()
        except OSError:
            is_file = False
        if is_file:
            private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            padding = "=" * (-len(value) % 4)
            try:
                raw = base64.urlsafe_b64decode(value + padding)
            except ValueError as exc:
                raise ValueError("VAPID 私钥不是有效的 PEM、路径或 base64url") from exc
            if len(raw) != 32:
                raise ValueError("VAPID base64url 私钥必须是 32 bytes")
            private_key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise ValueError("VAPID 私钥必须使用 P-256 曲线")
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")


def config() -> dict[str, Any]:
    private = os.environ.get("COCKPIT_VAPID_PRIVATE_KEY")
    public = os.environ.get("COCKPIT_VAPID_PUBLIC_KEY")
    if private or public:
        if not private or not public:
            return {
                "available": False,
                "reason": "COCKPIT_VAPID_PRIVATE_KEY 与 PUBLIC_KEY 必须同时设置",
            }
        try:
            import pywebpush  # noqa: F401
            derived_public = _public_key_from_private(private)
        except Exception as exc:
            return {"available": False, "reason": f"Web Push 初始化失败: {exc}"}
        if derived_public != public.rstrip("="):
            return {
                "available": False,
                "reason": "COCKPIT_VAPID_PRIVATE_KEY 与 PUBLIC_KEY 不匹配",
            }
        return {
            "available": True,
            "private_key": private,
            "public_key": public,
            "subject": VAPID_SUBJECT,
        }
    try:
        private, public = _ensure_private_key()
        import pywebpush  # noqa: F401
    except Exception as exc:
        return {"available": False, "reason": f"Web Push 初始化失败: {exc}"}
    return {
        "available": True,
        "private_key": private,
        "public_key": public,
        "subject": VAPID_SUBJECT,
    }


def public_config() -> dict[str, Any]:
    value = config()
    return {
        "available": value["available"],
        "public_key": value.get("public_key"),
        "reason": value.get("reason"),
    }


def _send(**kwargs: Any) -> Any:
    from pywebpush import webpush

    return webpush(**kwargs)


def notify(items: list[dict[str, Any]]) -> dict[str, int]:
    result = {"sent": 0, "removed": 0, "failed": 0}
    if not items:
        return result
    push_config = config()
    if not push_config.get("available"):
        logger.warning("Web Push 不可用: %s", push_config.get("reason", "未知原因"))
        return result

    def send_subscription(subscription: dict[str, Any]) -> dict[str, int]:
        counts = {"sent": 0, "removed": 0, "failed": 0}
        for item in items:
            payload = {
                "title": str(item.get("title") or "Agent Cockpit 需要你"),
                "body": str(item.get("detail") or "")[:240],
                "tag": str(item.get("id") or "agent-cockpit")[:160],
                "url": str(item.get("url") or "/"),
            }
            try:
                _send(
                    subscription_info={
                        "endpoint": subscription["endpoint"],
                        "keys": subscription["keys"],
                    },
                    data=json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=push_config["private_key"],
                    vapid_claims={"sub": push_config["subject"]},
                    ttl=120,
                    timeout=10,
                )
                counts["sent"] += 1
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    delete_subscription(subscription["endpoint"])
                    counts["removed"] += 1
                    break
                logger.warning(
                    "Web Push 发送失败(status=%s): %s", status or "unknown", exc
                )
                counts["failed"] += 1
        return counts

    subscriptions = list_subscriptions()
    workers = min(4, len(subscriptions))
    if not workers:
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for counts in executor.map(send_subscription, subscriptions):
            for key in result:
                result[key] += counts[key]
    return result
