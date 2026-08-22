"""C3 wiring: private Codex MCP subprocess 的组装根。

把 A 的 capability 绑定工具面（workspace_claim_tools / private_codex_mcp
dispatch）与 B 的执行件（ClaimActivator / WorkspaceWriteTools）组合成真实
MCP 服务。三处冻结接缝在此适配，不改 A/B 文件：

- ClaimContext（dataclass，无前缀键）-> ClaimActivator.activate 的 dict
  （expected_* 键）；
- router 的 (capability_file, arguments) -> WriteTools kwargs +
  派生 Idempotency-Key（capability token + 工具名 + 规范化参数；同意图同
  key，意图变化换 key，token 轮换自动失效）；
- main() 以真实 tools 注入 dispatch；库缺失/不可读时 fail-closed 为
  deny-all（不创建新库、不假装可用），故障态 tools/list 仍精确
  claim_current/apply_patch/reply_complete/submit_handoff 四项。

子进程只持有 capability 文件路径（COCKPIT_CAPABILITY_FILE）；库路径经
runtime_paths（尊重 COCKPIT_*_DIR env 根）或显式参数解析，token/fence/
pane 永不进入 MCP 响应。
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from . import local_codex_harness as harness_mod
from . import operation_store as operation_mod
from . import private_codex_mcp as mcp_mod
from . import runtime_paths
from . import workspace_claim_activation as activation_mod
from . import workspace_claim_tools as claim_mod
from . import workspace_execution_store as execution_mod
from . import workspace_delivery_service as delivery_service_mod
from . import workspace_delivery_store as delivery_store_mod
from . import workspace_work_store as work_mod
from . import workspace_write_tools as write_mod


class McpEntryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise McpEntryError(code)


def _arguments(value: object, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        _fail("invalid_argument")
    return value


def _derived_idempotency_key(
    capability_file: Path, tool: str, arguments: dict[str, object],
) -> str:
    try:
        record = harness_mod._read_capability(Path(capability_file))
    except harness_mod.HarnessError:
        _fail("runtime_capability_invalid")
    token = record.get("token")
    if not isinstance(token, str) or len(token) != 64:
        _fail("runtime_capability_invalid")
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    )
    digest = hashlib.sha256(f"{token}:{tool}:{canonical}".encode()).hexdigest()
    return f"mcp-{tool.replace('_', '-')}-{digest}"


class _ActivatorAdapter:
    """A 的 ClaimActivator Protocol（ClaimContext）-> B 的 dict 合同。"""

    def __init__(self, inner: activation_mod.ClaimActivator) -> None:
        self._inner = inner

    def activate(
        self, context: object, pending_claim: dict[str, object], *,
        idempotency_key: str,
    ) -> dict[str, object]:
        if not isinstance(context, claim_mod.ClaimContext):
            _fail("invalid_argument")
        mapped = {
            "project_id": context.project_id,
            "workspace_id": context.workspace_id,
            "work_item_id": context.work_item_id,
            "attachment_id": context.attachment_id,
            "identity_id": context.identity_id,
            "generation": context.generation,
            "expected_preparation_revision": context.preparation_revision,
            "expected_lease_revision": context.lease_revision,
            "expected_work_revision": context.work_revision,
        }
        return self._inner.activate(
            mapped, pending_claim, idempotency_key=idempotency_key,
        )


class _WriteToolsAdapter:
    """A router 的 (capability_file, arguments) -> B WriteTools kwargs。"""

    def __init__(self, inner: write_mod.WorkspaceWriteTools) -> None:
        self._inner = inner

    def apply_patch(
        self, capability_file: Path, arguments: dict[str, object],
    ) -> dict[str, object]:
        args = _arguments(
            arguments, frozenset({"claim_revision", "lease_revision", "patch"}),
        )
        return self._inner.apply_patch(
            capability_path=Path(capability_file),
            claim_revision=args["claim_revision"],
            lease_revision=args["lease_revision"],
            patch=args["patch"],
            idempotency_key=_derived_idempotency_key(
                Path(capability_file), "apply_patch", args,
            ),
        )

    def reply_complete(
        self, capability_file: Path, arguments: dict[str, object],
    ) -> dict[str, object]:
        args = _arguments(
            arguments, frozenset({"claim_revision", "lease_revision", "body"}),
        )
        return self._inner.reply_complete(
            capability_path=Path(capability_file),
            claim_revision=args["claim_revision"],
            lease_revision=args["lease_revision"],
            body=args["body"],
            idempotency_key=_derived_idempotency_key(
                Path(capability_file), "reply_complete", args,
            ),
        )

    def submit_handoff(
        self, capability_file: Path, arguments: dict[str, object],
    ) -> dict[str, object]:
        args = _arguments(
            arguments,
            frozenset({
                "claim_revision", "lease_revision", "summary", "test_evidence",
            }),
        )
        return self._inner.submit_handoff(
            capability_path=Path(capability_file),
            claim_revision=args["claim_revision"],
            lease_revision=args["lease_revision"], summary=args["summary"],
            test_evidence=args["test_evidence"],
            idempotency_key=_derived_idempotency_key(
                Path(capability_file), "submit_handoff", args,
            ),
        )


@dataclass
class McpTools:
    claim_tools: Any
    write_tools: Any
    close: Callable[[], None]


class _DeniedTools:
    """建库失败的 fail-closed 占位：tools/list 精确四项，调用全部拒绝。"""

    def __init__(self, code: str) -> None:
        self._code = code

    def claim_current(self, capability_file: Path) -> dict[str, object]:
        _fail(self._code)

    def apply_patch(
        self, capability_file: Path, arguments: dict[str, object],
    ) -> dict[str, object]:
        _fail(self._code)

    def reply_complete(
        self, capability_file: Path, arguments: dict[str, object],
    ) -> dict[str, object]:
        _fail(self._code)

    def submit_handoff(
        self, capability_file: Path, arguments: dict[str, object],
    ) -> dict[str, object]:
        _fail(self._code)


def build_tools(
    *,
    work_path: Path | None = None,
    execution_path: Path | None = None,
    operation_path: Path | None = None,
) -> McpTools:
    """打开既有三库并组装工具；任何一步失败即关闭已开库并抛错。"""
    work = work_mod.open_existing(
        Path(work_path)
        if work_path is not None
        else runtime_paths.validate_store("workspace_work")
    )
    try:
        execution = execution_mod.open_existing(
            Path(execution_path)
            if execution_path is not None
            else runtime_paths.validate_store("workspace_execution")
        )
        try:
            operations = operation_mod.open_existing(
                Path(operation_path)
                if operation_path is not None
                else runtime_paths.validate_store("operation_journal")
            )
        except BaseException:
            execution.close()
            raise
    except BaseException:
        work.close()
        raise
    activator = _ActivatorAdapter(
        activation_mod.ClaimActivator(
            execution=execution, work=work, operations=operations,
        )
    )
    claim_tools = claim_mod.WorkspaceClaimTools(
        work=work, execution=execution, activator=activator,
    )
    write_tools = _WriteToolsAdapter(
        write_mod.WorkspaceWriteTools(
            execution=execution, work=work, operations=operations,
            delivery_service=delivery_service_mod.WorkspaceDeliveryService(
                execution=execution,
                delivery=delivery_store_mod.WorkspaceDeliveryStore.from_work_store(
                    work
                ),
                source_path_provider=lambda _project, _workspace: Path("/"),
            ),
        )
    )

    def close() -> None:
        for store in (execution, work, operations):
            try:
                store.close()
            except Exception:
                pass

    return McpTools(claim_tools=claim_tools, write_tools=write_tools, close=close)


def serve(
    tools: McpTools | None, *, stdin: TextIO | None = None,
    stdout: TextIO | None = None, failure_code: str = "runtime_unavailable",
) -> int:
    """stdio JSON-RPC 循环；tools=None 时四项冻结工具面全部 fail-closed。"""
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    if tools is None:
        denied = _DeniedTools(failure_code)
        claim_tools: Any = denied
        write_tools: Any = denied
    else:
        claim_tools = tools.claim_tools
        write_tools = tools.write_tools
    for line in source:
        raw = line.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        reply = mcp_mod.dispatch(
            message, claim_tools=claim_tools, write_tools=write_tools,
        )
        if reply is not None:
            sink.write(json.dumps(reply, separators=(",", ":")) + "\n")
            sink.flush()
    return 0


def main() -> int:
    tools: McpTools | None = None
    failure_code = "runtime_unavailable"
    try:
        tools = build_tools()
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            failure_code = code
        tools = None
    try:
        return serve(tools, failure_code=failure_code)
    finally:
        if tools is not None:
            tools.close()


if __name__ == "__main__":
    raise SystemExit(main())
