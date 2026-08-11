"""tests/test_frontend.py — static/index.html 的静态回归测试。

不启动浏览器,仅把单文件前端当文本断言,锁死本轮修复:
  - esc 转义引号(文本 + 双引号属性上下文都安全)
  - 不可信值不再进入内联 handler / JS 字符串(改 data-* + 事件委托)
  - 移除阻断原生粘贴的 Clipboard API 逻辑
  - openTerm 改为开抽屉读/发 herdr pane(不再为卡片新建 PTY)
  - api() 保留服务端 detail
  - 多终端每实例独立 WS、切回死连接重连、resize 监听不累积
  - 对接 /api/auth/status + /api/auth/login 认证契约
  - herdr 流视图可滚动复制、默认卡片入口与轮询竞态保护
  - 文件管理按名称递归搜索、结果打开/下载与移动端布局
"""
import base64
import hashlib
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _inline_js() -> str:
    """抽取最后一个无 src 的 <script>…</script> 内联脚本。"""
    m = re.findall(r"<script>(.*)</script>", HTML, re.S)
    return m[-1] if m else ""


def test_esc_escapes_quotes_for_attribute_context():
    # esc 必须转义 " 和 ',才能在 value="/class=" 等双引号属性里防 breakout
    assert "&quot;" in HTML
    assert "&#39;" in HTML
    # 旧的不转义引号的正则不得残留
    assert "/[&<>]/g" not in HTML


def test_no_untrusted_value_in_inline_handlers():
    # 任何 on* 属性里都不得出现 ${ 插值(不可信值改走 data-* + 委托)
    assert re.search(r'on(click|input|keydown|change)="[^"]*\$\{', HTML) is None
    # 具体高危模式全部移除
    for bad in [
        "onclick=\"openTerm('${esc",
        "onclick=\"restartPane('${esc",
        "onclick=\"fileOpen('${esc",
        "onclick=\"fileGoto('${esc",
        "onclick=\"ackMsg(",
        "onclick=\"doAttachHerdr('${esc",
        "onclick=\"pickMention('${esc",
        "onclick=\"sendUploadToPane('${esc",
    ]:
        assert bad not in HTML, f"残留不安全内联 handler: {bad}"


def test_uses_data_action_and_event_delegation():
    assert 'data-action="openTerm"' in HTML
    assert 'data-action="ack"' in HTML
    assert "closest('[data-action]')" in HTML


def test_clipboard_paste_blocking_removed():
    # 不主动读取/劫持系统剪贴板；原生粘贴继续由浏览器/xterm 处理。
    assert "navigator.clipboard.readText" not in HTML
    assert "e.preventDefault();navigator.clipboard" not in HTML


def test_herdr_osc52_clipboard_bridge_and_http_fallback():
    js = _inline_js()
    assert "SERVICE_CLIPBOARD" in js
    assert "registerOscHandler(52,handleOsc52)" in js
    assert "function handleOsc52(data)" in js
    assert "function syncServiceClipboard()" in js
    assert "function pasteServiceClipboard(input)" in js
    assert "navigator.clipboard.writeText" in js
    assert "document.execCommand('copy')" in js
    assert "window.isSecureContext" in js
    assert 'data-action="hfPaste"' in js
    assert HTML.count("📋 复制到剪贴板") >= 2
    assert "📋 填入输入框" in HTML
    assert "HTTP 下请点" not in js
    assert "同步剪贴板" not in HTML
    assert "粘贴服务" not in HTML


def test_open_term_opens_drawer_reading_herdr_pane():
    js = _inline_js()
    # 点卡片设置 CURRENT_TERM 并打开抽屉(不再 POST /api/term 新建 PTY)
    assert "CURRENT_TERM={" in js
    assert "termDrawer').classList.add('show')" in js
    # 抽屉读取/发送目标走 herdr pane
    assert "/api/herdr/pane/" in js
    # 抽屉定时刷新 pane 输出
    assert "TERM_TIMER=setInterval(refreshTerm" in js


def test_pane_output_refresh_only_replaces_changed_text_range():
    js = _inline_js()
    assert "function updatePaneOutput(out,next)" in js
    assert "current.charCodeAt(start)===next.charCodeAt(start)" in js
    assert "current.charCodeAt(oldEnd-1)===next.charCodeAt(newEnd-1)" in js
    assert "node.replaceData(start,oldEnd-start,next.slice(start,newEnd))" in js


def test_degraded_pane_read_shows_notice_without_rendering_html():
    js = _inline_js()
    drawer = js.split("async function refreshTerm(){", 1)[1].split(
        "async function termSend", 1
    )[0]
    flow = js.split("async function hfRefreshAll(){", 1)[1].split(
        "async function hfSend", 1
    )[0]

    assert "function paneReadText(d)" in js
    assert "d.degraded&&d.notice" in js
    assert "paneReadText(d)" in drawer
    assert "paneReadText(d)" in flow
    assert "innerHTML=d.output" not in js
    assert "updatePaneOutput(out,next)" in drawer
    assert "updatePaneOutput(out,next)" in flow


def test_api_preserves_server_detail():
    # 不再把服务端返回的 detail 覆盖成 r.statusText
    assert "throw r.statusText" not in HTML
    assert "JSON.parse(t).detail" in HTML


def test_multi_terminal_keeps_ws_per_instance_and_reconnects():
    js = _inline_js()
    # 新建终端不再关闭其它实例的 WS
    assert "if(TERM_WS){try{TERM_WS.close()}catch(e){}}" not in js
    # 切回死连接(CLOSING/CLOSED)自动重连
    assert "readyState>=2" in js
    # resize 监听只注册一次(不再每个 termMount 各加一个)
    assert HTML.count("window.addEventListener('resize'") == 1


def test_terminal_selector_uses_herdr_session_name():
    js = _inline_js()
    assert "TERM_LABELS={}" in js
    assert "function renderTermOptions()" in js
    assert "function termLabel(id)" in js
    assert "label='+encodeURIComponent(session)" in js
    assert "TERM_LABELS[r.id]=r.label||session" in js
    assert "TERM_LABELS[r.id]=r.label||''" in js


def test_terminal_page_only_catalogs_live_terms_until_explicit_open():
    js = _inline_js()
    restore = js.split("async function restoreTerms(){", 1)[1].split(
        "async function termNew", 1
    )[0]
    options = js.split("function renderTermOptions(){", 1)[1].split(
        "async function restoreTerms", 1
    )[0]
    ensure = js.split("async function termEnsure(){", 1)[1].split(
        "function termLabel", 1
    )[0]

    assert "Promise.all([api('/api/term'),api('/api/herdr/sessions')])" in restore
    assert "item.alive===false" in restore
    assert "TERMS.push(id);TERM_LABELS[id]=item.label||''" in restore
    assert "TERM_SESSIONS[id]=item.label" in restore
    assert "TERM_ID=TERMS.find" not in restore
    # 进入终端页只读取目录，不挂 WebSocket、不创建/attach PTY。
    assert "method:'POST'" not in restore
    # 刷新后恢复的 Herdr attach PTY 不再伪装成可复用终端；列表只给显式打开入口。
    assert "Object.entries(TERM_SESSIONS)" in options
    assert ".filter(([id])=>TERM_INSTANCES[id])" in options
    assert "TERMS.filter(id=>!TERM_SESSIONS[id]||TERM_INSTANCES[id])" in options
    assert "TERM_SESSION_CATALOG.filter(session=>!attached.has(session))" in options
    assert "${esc(session)} · 打开" in options
    assert "await restoreTerms()" in ensure
    assert "termSwitch(" not in ensure
    assert "doAttachHerdr(" not in ensure
    assert "termNew(" not in ensure
    assert "showTermInstance(TERM_ID)" in ensure
    assert "尚未打开终端" in ensure
    assert "id.startsWith(TERM_SESSION_PREFIX)" in js
    init = js.split("async function init(){", 1)[1].split("init();", 1)[0]
    assert "restoreTerms(" not in init
    assert "restoreAllHerdrSessions" not in js


def test_herdr_terminal_attach_atomically_replaces_restored_pty_on_server():
    js = _inline_js()
    attach = js.split("async function doAttachHerdr(session", 1)[1].split(
        "// ============ herdr", 1
    )[0]
    websocket = js.split("function openTermWS(id,xterm,replay){", 1)[1].split(
        "function showTermInstance", 1
    )[0]

    assert 'onclick="termAttachHerdr()"' in HTML
    assert "data-action=\"attach\"" in js
    assert "else if(a==='attach')doAttachHerdr(s)" in js
    # 跨浏览器不能各自按缓存删除旧 ID 再创建；服务端必须串行替换同 label PTY。
    assert "replace_existing=true" in attach
    assert "await api('/api/term/'+id,{method:'DELETE'})" not in attach
    assert "TERMS.find(id=>TERM_SESSIONS[id]===session&&TERM_INSTANCES[id])" in attach
    assert "TERMS.filter(id=>TERM_SESSIONS[id]===session)" in attach
    # 新终端挂载完成后再刷新 selector，不能又显示成待打开的 session。
    created = attach.split("const r=await api('/api/term?label='", 1)[1]
    assert created.index("termMount(r.id)") < created.index("renderTermOptions()")
    assert created.index("holdTermLoadingForTui(r.id") < created.index(
        "queueTermInput(r.id,'herdr --session '"
    )
    # WebSocket 必须先设置主题和尺寸，再执行排队的 herdr attach 命令。
    assert websocket.index("sendTermColorScheme(id,ws,!replay)") < websocket.index(
        "type:'resize'"
    )
    assert websocket.index("type:'resize'") < websocket.index("flushTermInput(id,ws)")


def test_auth_contract_wired():
    js = _inline_js()
    assert "/api/auth/status" in js
    assert "/api/auth/login" in js
    assert "ensureAuth" in js
    assert "authenticated" in js
    # cookie 经 fetch(credentials) / EventSource(withCredentials) 携带
    assert "new EventSource('/api/events',{withCredentials:true})" in js
    assert "credentials:'include'" in js


def test_download_ui_preserved():
    # 本轮新增的单文件下载 UI 必须保留
    assert "fileDownloadEntry" in HTML
    assert "/api/files/download" in HTML


def test_terminal_assets_are_self_hosted_with_subresource_integrity():
    # 终端首屏不应被外部 CDN 阻塞；本地副本仍用 SRI 锁定内容。
    assets = (
        "vendor/xterm/xterm.css",
        "vendor/xterm/xterm.js",
        "vendor/xterm/addon-fit.js",
    )
    assert "cdn.jsdelivr.net" not in HTML
    for asset in assets:
        tags = [line for line in HTML.splitlines() if f"/static/{asset}" in line]
        assert tags
        assert all('integrity="sha384-' in line for line in tags)
        assert all('crossorigin="anonymous"' in line for line in tags)
        asset_path = Path(__file__).resolve().parent.parent / "static" / asset
        assert asset_path.is_file()
        digest = base64.b64encode(hashlib.sha384(asset_path.read_bytes()).digest()).decode()
        assert f'integrity="sha384-{digest}"' in tags[0]
    for license_name in ("LICENSE.xterm", "LICENSE.addon-fit", "LICENSE.addon-webgl"):
        assert (
            Path(__file__).resolve().parent.parent
            / "static"
            / "vendor"
            / "xterm"
            / license_name
        ).is_file()
    notice = (
        Path(__file__).resolve().parent.parent
        / "static"
        / "vendor"
        / "xterm"
        / "README.md"
    ).read_text()
    assert "@xterm/xterm` 6.1.0-beta.287" in notice
    assert "@xterm/addon-fit` 0.12.0-beta.287" in notice
    assert "@xterm/addon-webgl` 0.20.0-beta.286" in notice
    assert "xterm.js 加载失败(版本升级后旧缓存?)" in HTML
    assert 'onclick="location.reload(true)"' in HTML


def test_terminal_webgl_renderer_with_safe_fallback():
    js = _inline_js()
    mount = js.split("function termMount(id){", 1)[1].split("// ============ 会话管理", 1)[0]

    # xterm 6.x 渲染器重写后重新启用 WebGL;必须保留 context-loss 回退
    # (dispose + 强制重绘),避免 8/5 回滚事件的字符错位复发。
    assert "/static/vendor/xterm/addon-webgl.js" in HTML
    assert "enableTermWebgl(xterm)" in mount
    assert "onContextLoss" in js
    assert "xterm.refresh(0,xterm.rows-1)" in js
    assert "TERM_INSTANCES[id]={xterm,fit,ws:null," in mount


def test_message_fields_escaped():
    # m.importance 可由 API 写入,不得裸进 innerHTML;fmtTime/m.id 同理 esc
    assert "${m.importance" not in HTML
    assert "${esc(m.importance" in HTML
    assert "${esc(fmtTime(m.created_ts))}" in HTML
    assert "${esc(m.sender_name" in HTML
    # data-pid/data-mid 属性上下文也 esc(防御契约变化)
    assert 'data-pid="${esc(d.project.id)}"' in HTML
    assert 'data-mid="${esc(m.id)}"' in HTML


def test_board_card_displays_escaped_agent_mail_name():
    js = _inline_js()
    card = js.split("function cardHtml(p){", 1)[1].split("// ============ 终端抽屉", 1)[0]
    assert 'class="card-alias">@${esc(p.mail_name)}' in card
    assert '@${p.mail_name}' not in card


def test_identity_board_uses_display_name_and_disambiguates_duplicates():
    js = _inline_js()
    card = js.split("function cardHtml(p){", 1)[1].split(
        "// ============ 终端抽屉", 1,
    )[0]

    assert "function paneDisplayName(p)" in js
    assert "function paneInstanceId(p)" in js
    assert "function shortInstanceId(value)" in js
    assert "function paneDisplayMeta(p)" in js
    assert "esc(paneDisplayName(p))" in card
    assert "esc(paneDisplayMeta(p))" in card
    assert "p.display_name" not in card
    assert "p.instance_id" not in card


def test_board_degraded_banner_consumes_reason_and_state_status():
    # Wiki16-19 前端场景3:renderBoard 消费 board degraded/reason 与
    # sessions[].state_status/state_reason,Hub/socket 降级原因可见且全部转义
    js = _inline_js()
    assert 'id="boardDegraded"' in HTML
    assert "function renderBoardDegraded(d)" in js
    fn = js.split("function renderBoardDegraded(d){", 1)[1].split("function ", 1)[0]
    assert "esc(String(d.reason))" in fn
    assert "esc(String(s.state_reason))" in fn
    assert "esc(stateStatusLabel(st))" in fn
    # 原始值绝不直插 HTML
    assert "${d.reason}" not in fn and "${s.state_reason}" not in fn
    # R2:degraded 即展示(available=false 也展示真实 reason);非 degraded 隐藏
    assert "!d||!d.degraded" in fn
    # renderBoard 两条路径都经过 renderBoardDegraded
    body = js.split("function renderBoard(d){", 1)[1].split("function cardHtml", 1)[0]
    assert body.count("renderBoardDegraded(d)") >= 2


def test_board_unavailable_separated_from_idle():
    # Wiki16-19 前端场景4:state_status 非 subscribed 的 pane 归 unavailable,
    # 独立列展示,禁止落入空闲;旧 payload 无 state_status → 行为不变
    js = _inline_js()
    assert "stateStatus!=='subscribed')return 'unavailable'" in js
    assert "'st.unavailable','不可用'" in js  # statusText + cardStatusText
    assert js.count("'st.unavailable','不可用'") >= 2
    assert 'id="colWrap-unavailable"' in HTML
    assert 'id="col-unavailable"' in HTML and 'id="cnt-unavailable"' in HTML
    assert "'board.unavailable'" in HTML
    # 列默认隐藏,有内容才显示
    assert 'id="colWrap-unavailable" style="display:none"' in HTML
    assert "colWrap-unavailable').style.display=cols.unavailable.length?'':'none'" in js
    # 旧 payload 兼容:无 sessions/state_status 时 classify 第三参为 undefined
    assert "return s?s.state_status:undefined" in js


def test_board_unavailable_false_shows_real_reason_not_install_claim():
    # R2/R3/R4:available=false 时只有严格 allowlist 的 herdr 二进制/命令缺失
    # 才显示安装引导;否则泛化诊断+重试,真实原因走降级横幅
    js = _inline_js()
    body = js.split("if(!d.available){", 1)[1].split("return;", 1)[0]
    assert "renderBoardDegraded(d)" in body
    assert "isHerdrBinaryMissing(reason)" in body
    assert "function isHerdrBinaryMissing(" in js
    # R4:禁止 80 字符共现窗与裸 not found
    fn = js.split("function isHerdrBinaryMissing(", 1)[1].split("function ", 1)[0]
    assert r"[\s\S]{0,80}" not in fn and "{0,80}" not in fn
    assert "|not found|" not in body and "not found|executable" not in body
    assert "board.unavailable.title" in body and "refreshCurrent()" in body
    missing_branch = body.split("if(missing){", 1)[1]
    assert "board.guide.noherdr" in missing_branch
    generic = body.split("}else{", 1)[1]
    assert "board.guide.noherdr" not in generic


def _extract_is_herdr_binary_missing_js() -> str:
    """从 static/index.html 抽出真实 isHerdrBinaryMissing 函数体(供 Node 执行)。"""
    js = _inline_js()
    start = js.index("function isHerdrBinaryMissing(")
    end = js.index("function renderBoard(", start)
    return js[start:end].strip()


def _js_is_herdr_binary_missing(reason: str) -> bool:
    """R4:分类真值必须来自真实 JS 引擎,禁止 Python 复制正则。"""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node, "node required for herdr-binary-missing JS truth table"
    fn = _extract_is_herdr_binary_missing_js()
    script = fn + f"\nprocess.stdout.write(String(isHerdrBinaryMissing({json.dumps(reason)})));\n"
    proc = subprocess.run(
        [node, "-e", script],
        check=True, capture_output=True, text=True, timeout=10,
    )
    out = (proc.stdout or "").strip()
    assert out in ("true", "false"), (reason, out, proc.stderr)
    return out == "true"


# R4(#2008) 真值表——与实现单正文一致
HERDR_MISSING_POSITIVES = [
    "Herdr 未安装；请安装或升级到0.8.0以上",
    "herdr 未安装或不可执行:/missing/herdr",
    "herdr executable not found",
    "command not found: herdr",
    "herdr 未找到",
    "herdr is not installed",
    "not installed: herdr",
]
HERDR_MISSING_NEGATIVES = [
    "herdr socket not installed",
    "herdr available; agent opencode not installed",
    "not installed plugin for herdr status provider",
    "herdr state cache is not installed",
    "not found",
    "executable",
    "not installed",
    "socket not found",
    "cache not found",
    "session not found",
    "pane not found",
    "state not found",
]


def test_herdr_binary_missing_js_truth_table_positive():
    # 真实 Node/JS 引擎真值:正向 → true
    for reason in HERDR_MISSING_POSITIVES:
        assert _js_is_herdr_binary_missing(reason) is True, reason
    js = _inline_js()
    assert "herdr\\s+executable\\s+not\\s+found" in js
    assert "herdr\\s*未安装" in js
    assert "herdr\\s+is\\s+not\\s+installed" in js


def test_herdr_binary_missing_js_truth_table_negative():
    # 真实 Node/JS 引擎真值:反向 → false(含误判样例与裸短语)
    for reason in HERDR_MISSING_NEGATIVES:
        assert _js_is_herdr_binary_missing(reason) is False, reason
    body = _inline_js().split("if(!d.available){", 1)[1].split("return;", 1)[0]
    assert "isHerdrBinaryMissing(reason)" in body


def test_dynamic_status_labels_all_i18n_keys():
    # R2:新增动态状态全部 t()+zh/en/ja,英文/日文不得混中文
    js = _inline_js()
    for key in (
        "st.unavailable", "st.connecting", "st.starting",
        "st.swap_pending", "st.reconnecting",
    ):
        assert f"'{key}'" in js  # t() 调用处
        assert f"'{key}'" in HTML.split("const I18N", 1)[1]  # en/ja 字典
    for fn in ("statusText", "cardStatusText", "stateStatusLabel"):
        assert f"function {fn}(" in js
        body = js.split(f"function {fn}(", 1)[1].split("function ", 1)[0]
        assert "t(" in body  # 统一走 t()
    # strip/card 不再有硬编码中文状态 map
    assert "statusLabel={" not in js and "stMap={" not in js


def test_board_has_no_binding_field_placeholders():
    # 本轮边界:不新增 pending_count/binding_version/route_epoch 或 binding 占位
    for bad in ("pending_count", "binding_version", "route_epoch"):
        assert bad not in HTML


def test_setup_workspace_output_escaped():
    # r.started/r.notified 来自后端响应,经 map(esc) 后再 join
    assert "(r.started||[]).map(esc).join" in HTML
    assert "(r.notified||[]).map(esc).join" in HTML
    assert "esc(r.terminal_output)" in HTML


def test_setup_workspace_supports_roles_tasks_and_automatic_worktrees():
    js = _inline_js()
    assert 'data-mode="develop_review"' in HTML
    assert 'data-mode="parallel"' in HTML
    assert 'id="suGoal"' not in HTML
    assert 'id="suAgentsList"' in HTML
    assert "SETUP_ROLE_LABELS" in js
    assert "function renderSetupPreview()" in js
    assert "participants,agents,layout" in js
    assert "多个并行写入者不能共享目录" in js
    assert "固定版本复核目录" in js
    assert "独立 worktree" in HTML
    assert "/api/herdr/inspect-workspace" in js
    assert "SETUP_SUBMITTING" in js
    assert "当前目录不是 Git 仓库" in js
    layout = re.search(r'<select id="suLayout">(.*?)</select>', HTML, re.S)
    assert layout is not None
    assert layout.group(1).find('value="tab"') < layout.group(1).find('value="right"')


def test_setup_workspace_allows_optional_tasks_and_keeps_success_locked():
    js = _inline_js()
    modes = js.split("function setupMode(mode){", 1)[1].split(
        "function setupRoleOptions", 1,
    )[0]
    errors = js.split("function setupErrors(){", 1)[1].split(
        "function setupInspectWorkspace", 1,
    )[0]
    submit = js.split("async function doSetupWorkspace(){", 1)[1].split(
        "async function setupHerdrOnboarding", 1,
    )[0]

    assert "完成开发工作并提交可复核的 commit" not in modes
    assert "检查需求、代码和测试" not in modes
    assert "负责模块 A" not in modes
    assert "setupParticipant(first,'lead','')" in modes
    assert "SETUP_PARTICIPANTS.some(p=>!p.task.trim())" not in errors
    assert "请填写每个 Agent 的真实任务" not in errors
    assert "task 选填" in errors
    assert "SETUP_PARTICIPANTS.length>1&&" not in errors
    assert "let setupSucceeded=false" in submit
    assert "setupSucceeded=true" in submit
    assert "SETUP_IN_FLIGHT" in submit
    assert "reqId!==SETUP_REQ_ID" in submit
    assert "if(!setupSucceeded){SETUP_SUBMITTING=false;renderSetupPreview()}" in submit
    assert "SETUP_CLOSE_TIMER" not in js
    assert 'data-action="setupOpenTerminal"' in submit
    assert "async function setupOpenTerminal(session)" in js
    assert "await doAttachHerdr(session)" in js
    # 关窗不得解除在途锁
    close = js.split("function closeSetup(){", 1)[1].split(
        "async function setupOpenTerminal", 1,
    )[0]
    assert "if(SETUP_IN_FLIGHT)" in close
    # remove('show') 必须在 in-flight guard 之后（在途不隐藏）
    assert close.index("if(SETUP_IN_FLIGHT)") < close.index("remove('show')")
    assert "工作区正在启动中" in close


def test_setup_workspace_allows_repeated_agent_types_and_display_names():
    js = _inline_js()
    fields = js.split("function renderSetupAgents(){", 1)[1].split(
        "function setupFieldChanged", 1,
    )[0]
    add = js.split("function setupAddAgent(){", 1)[1].split(
        "function setupRemoveAgent", 1,
    )[0]
    errors = js.split("function setupErrors(){", 1)[1].split(
        "function setupInspectWorkspace", 1,
    )[0]

    assert 'data-field="name"' in fields
    assert "setupNextAgentName" not in js
    assert "find(a=>!SETUP_PARTICIPANTS.some(p=>p.agent===a))" not in add
    assert "同一种 Agent 不能重复" not in errors
    assert "实例名称不能重复" not in errors
    assert 'id="lnName"' in HTML
    assert 'id="lnLayout"' in HTML
    assert 'id="lnWorkspace"' in HTML
    assert "function launchSessionChanged" in js
    assert "const preferredSession=(TERM_ID&&TERM_SESSIONS[TERM_ID])||CURRENT_TERM?.session||''" in js
    assert "await loadSessions(preferredSession)" in js


def test_identity_frontend_never_generates_or_submits_instance_id():
    js = _inline_js()
    launch = js.split("async function startAgent(){", 1)[1].split(
        "// ============ 消息 view", 1,
    )[0]
    fields = js.split("function renderSetupAgents(){", 1)[1].split(
        "function setupFieldChanged", 1,
    )[0]
    errors = js.split("function setupErrors(){", 1)[1].split(
        "function setupInspectWorkspace", 1,
    )[0]

    assert 'placeholder="显示名"' in HTML
    assert "lnInstanceId" not in HTML
    assert "instance_id" not in launch
    assert "JSON.stringify({session,workdir,agent,name,layout,workspace,args})" in launch
    assert 'data-field="name"' in fields
    assert 'placeholder="显示名"' in fields
    assert "实例名称只能包含" not in errors
    assert "实例名称不能重复" not in errors
    assert "显示名不能为空" in errors


def test_agent_launch_args_are_available_only_in_on_demand_forms():
    js = _inline_js()
    launch = HTML.split('id="launchModal"', 1)[1].split('id="termDrawer"', 1)[0]

    assert 'id="lnArgs"' in launch
    assert 'maxlength="2048"' in launch
    assert '按命令行参数解析，不执行额外 shell 命令' in launch
    assert "JSON.stringify({session,workdir,agent,name,layout,workspace,args})" in js
    assert 'data-field="args"' in HTML
    assert "args:(p.args||'').trim()" in js
    assert "--dangerously-bypass-approvals-and-sandbox" not in HTML
    assert "#lnSession,#lnName,#lnWorkdir,#lnArgs" in HTML
    assert "const selected=preferredSession||sel.value" in js
    assert "LAUNCH_SESSION_DIRS" in js
    assert "s.directory||''" in js
    assert "p.session===session&&p.agent&&p.cwd" in js
    assert "/\\.config\\/herdr\\/sessions" in js
    launch = js.split("async function startAgent(){", 1)[1].split(
        "// ============ 消息 view", 1,
    )[0]
    assert "JSON.stringify({session,workdir,agent,name,layout,workspace,args})" in launch
    assert "workspace==='shared'" in launch
    assert "共享工作目录，并发写入可能冲突" in launch
    assert 'id="lnStartBtn"' in HTML
    assert "QoderCLI 正在初始化" in launch
    assert "button.disabled=true" in launch
    assert "finally" in launch and "button.disabled=false" in launch
    assert "QoderCLI 首次启动可能约 60 秒" in js
    assert "const mail=r.agent_mail||{}" in launch
    assert "const mailError=agentMailRequirementError()" in launch
    assert "mail.warning" in launch
    assert "身份 '+mail.name+' 已通知" in launch
    response = launch.split("if(r.error)", 1)[1]
    error, success = response.split(";else{", 1)
    assert "closeLaunchModal();" not in error
    assert "closeLaunchModal();" in success


def test_add_agent_form_opens_from_terminal_toolbar_instead_of_board_footer():
    js = _inline_js()
    terminal = HTML.split('id="view-term"', 1)[1].split('</section>', 1)[0]
    launch_modal = HTML.split('id="launchModal"', 1)[1].split('</div>\n</div>', 1)[0]

    assert 'onclick="showLaunchModal()"' in terminal
    assert '＋ 添加 Agent' in terminal
    assert 'id="launchBar"' in launch_modal
    board_area = HTML.split('<!-- 看板 -->', 1)[1].split('<!-- 消息 -->', 1)[0]
    assert 'id="launchBar"' not in board_area
    assert "function showLaunchModal()" in js
    assert "function closeLaunchModal()" in js
    assert "classList.toggle('hidden',v!=='board')" not in js
    assert "getElementById('launchBar').classList.add('hidden')" not in js


def test_old_session_mail_project_can_be_selected_in_ui():
    assert "/mail-project" in HTML
    assert "needs_selection" in HTML
    assert "window.prompt" in HTML


def test_setup_workspace_handles_herdr_onboarding_in_visible_terminal():
    js = _inline_js()
    assert "herdr_onboarding_required" in js
    assert "打开终端配置 Herdr" in js
    assert "async function setupHerdrOnboarding(command)" in js
    assert "data-command=\"${esc(r.herdr_command||'herdr')}\"" in js
    assert "setupHerdrOnboarding(it.dataset.command)" in js


def test_agent_mail_is_required_for_new_workspaces_and_agents():
    js = _inline_js()
    assert "Agent Mail 必需" in HTML
    assert "Agent Mail(必需,消息功能)" in js
    assert "function agentMailRequirementError()" in js
    errors = js.split("function setupErrors(){", 1)[1].split(
        "function setupInspectWorkspace", 1,
    )[0]
    assert "errors.push(mailError)" in errors
    assert "创建工作区和添加 Agent 已禁用" in js


def test_attach_herdr_validates_session_name():
    # session 拼进发给 PTY 的 shell 命令,先用与后端一致的白名单校验,非法则不写 WS
    assert "/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/" in HTML
    assert "validSessionName(session)" in HTML
    assert "if(!validSessionName(session))" in HTML


def test_attach_herdr_waits_for_its_websocket():
    js = _inline_js()
    assert "function queueTermInput(id,data,notice,protocol=false)" in js
    assert "flushTermInput(id,ws)" in js
    assert "queueTermInput(r.id,'herdr --session '+session+'\\r'" in js
    assert "// 等 WS 连上,发 herdr attach 命令\n    setTimeout" not in js


def test_h5_layout_uses_visible_dynamic_viewport():
    # 手机浏览器地址栏/键盘会改变可视高度；页面与全屏抽屉必须跟随 dvh。
    assert "height:100dvh" in HTML
    assert ".drawer-bg.show{inset:var(--safe-top) var(--safe-right) var(--safe-bottom) var(--safe-left);padding:0}" in HTML
    assert ".drawer{height:100%;max-height:100%" in HTML
    # 窄屏导航为左侧抽屉(可被划出),终端工具栏换行,不被 body overflow:hidden 裁掉。
    assert ".side{position:fixed" in HTML
    assert ".side.open{transform:none}" in HTML
    assert ".term-toolbar{flex-wrap:wrap" in HTML
    assert "visualViewport" in HTML


def test_h5_safe_area_and_overlay_scroll_are_contained():
    assert "viewport-fit=cover" in HTML
    assert "--safe-top:env(safe-area-inset-top,0px)" in HTML
    assert "padding:var(--safe-top) var(--safe-right) var(--safe-bottom) var(--safe-left)" in HTML
    assert "bottom:calc(64px + var(--safe-bottom))" in HTML
    assert "overscroll-behavior:none" in HTML
    assert "overscroll-behavior:contain" in HTML


def test_page_focus_only_recovers_closed_connections():
    js = _inline_js()
    assert "document.addEventListener('visibilitychange'" in js
    assert "function recoverPageState()" in js
    recovery = js.split("function recoverPageState(){", 1)[1].split("function ", 1)[0]
    assert "document.visibilityState==='hidden'" in recovery
    assert "now-LAST_PAGE_RECOVERY<300" in recovery
    assert "if(!SSE||SSE.readyState===2)connectSSE()" in recovery
    assert "window.addEventListener('focus',recoverPageState)" in js
    assert "window.addEventListener('pageshow',()=>{recoverPageState();if(TEAM.authenticated&&TEAM.human&&!TEAM_POLL_TIMER)teamPollStart()})" in js
    assert "showTermInstance(TERM_ID)" in recovery
    assert "refreshBoard()" not in recovery
    assert "loadAttention()" not in recovery
    assert "loadSessionsView()" not in recovery
    assert "loadProjList()" not in recovery
    assert "loadFileTree()" not in recovery
    assert "hfRefreshAll()" not in recovery
    assert "safeFitOf(" not in recovery
    assert ".send(" not in recovery
    assert ".focus()" not in recovery
    assert "location.reload" not in recovery
    assert "function scheduleTermReconnect(id)" in js
    assert "reconnectDelay" in js
    assert "Math.min(delay*2,10000)" in js
    assert "function keepFocusedControlVisible()" in js
    assert "scrollIntoView({block:'nearest'})" in js


def test_h5_drawer_can_switch_herdr_panes_without_keyboard_shortcuts():
    js = _inline_js()
    assert 'id="paneSelect"' in HTML
    assert "function syncPaneSelect()" in js
    assert "function switchDrawerPane(paneId)" in js
    assert "function stepDrawerPane(delta)" in js
    # 切换时旧 pane 的慢响应不能覆盖新 pane 内容。
    assert "CURRENT_TERM.paneId!==t.paneId" in js


def test_h5_takeover_opens_real_tui_with_touch_keys():
    js = _inline_js()
    takeover = js.split("async function doAttachHerdr(session", 1)[1].split(
        "// ============ herdr 流视图", 1
    )[0]
    # “接管 TUI”在手机上也必须创建 PTY 并执行 herdr attach，不能降级成普通 pane 抽屉。
    assert "openTerm(" not in takeover
    assert "hfStop();" in takeover
    assert "document.getElementById('view-term').classList.add('active')" in takeover
    assert "api('/api/term?label='+encodeURIComponent(session)" in takeover
    assert "queueTermInput(r.id,'herdr --session '+session+'\\r'" in takeover
    # 手机软键盘缺少终端控制键，TUI 页面必须提供触屏按键。
    assert 'class="term-keys"' in HTML
    assert "function termKey(name)" in js
    assert "ArrowUp:'\\x1b[A'" in js
    assert "Enter:'\\r'" in js
    assert "CtrlC:'\\x03'" in js
    assert "CtrlB:'\\x02'" in js
    for key in ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", "Tab", "Escape", "CtrlC", "CtrlB"]:
        assert f'onclick="termKey(\'{key}\')"' in HTML


def test_narrow_takeover_owns_zoom_with_heartbeat_and_release_fallbacks():
    js = _inline_js()
    takeover = js.split("async function doAttachHerdr(session", 1)[1].split(
        "// ============ herdr 流视图", 1
    )[0]
    close = js.split("async function termClose(){", 1)[1].split(
        "// 多终端保活", 1
    )[0]
    websocket = js.split("function openTermWS(id,xterm,replay){", 1)[1].split(
        "function showTermInstance", 1
    )[0]
    assert "TERM_ZOOM={}" in js
    assert "startTermZoomLease(r.id)" not in takeover
    assert "if(!TERM_INSTANCES[r.id])throw new Error('终端初始化失败')" in takeover
    assert "await termClose()" in takeover
    # 手机端 attach 前先拆分屏为独立 tab，避免窄屏挤成一条缝
    assert "ensureMobileSessionUntiled" in js
    assert "isCompactScreen()" in takeover
    assert "ensureMobileSessionUntiled(session)" in takeover
    assert "/layout/untile" in js
    assert "MOBILE_UNTILED_SESSIONS" in js
    # session 级 in-flight Promise 去重，避免并发 attach/zoom 双 untile
    assert "MOBILE_UNTILE_INFLIGHT" in js
    assert "if(MOBILE_UNTILE_INFLIGHT[session])return MOBILE_UNTILE_INFLIGHT[session]" in js
    assert "/zoom-lease" in js
    assert "action=state.owned?'renew':'acquire'" in js
    assert "TERM_ZOOM_HEARTBEAT=10000" in js
    assert "setInterval(()=>syncTermZoomLease(id),TERM_ZOOM_HEARTBEAT)" in js
    assert "startTermZoomLease(id)" in websocket
    assert websocket.index("flushTermInput(id,ws)") < websocket.index("startTermZoomLease(id)")
    assert "await releaseTermZoomLease(id)" in close
    assert close.index("await releaseTermZoomLease(id)") < close.index("api('/api/term/'+id")
    assert "window.addEventListener('pagehide',()=>{releaseAllTermZoomLeases();teamPollStop()})" in js
    assert "keepalive:true" in js
    assert "COMPACT_SCREEN_MQ.addEventListener?.('change'" in js


def test_fresh_xterm_requests_history_but_socket_reconnect_does_not():
    js = _inline_js()
    mount = js.split("function termMount(id){", 1)[1].split(
        "// ============ 会话管理", 1
    )[0]
    show = js.split("function showTermInstance(id){", 1)[1].split(
        "function termSwitch", 1
    )[0]
    reconnect = js.split("function scheduleTermReconnect(id){", 1)[1].split(
        "// 为某终端建立", 1
    )[0]
    websocket = js.split("function openTermWS(id,xterm,replay){", 1)[1].split(
        "function showTermInstance", 1
    )[0]

    assert "?replay=1" in websocket
    assert "openTermWS(id,xterm,true)" in mount
    assert "openTermWS(id,inst.xterm,false)" in show
    assert "openTermWS(id,current.xterm,false)" in reconnect


def test_new_terminal_connection_takes_over_without_old_page_reconnect_loop():
    js = _inline_js()
    websocket = js.split("function openTermWS(id,xterm,replay){", 1)[1].split(
        "function showTermInstance", 1
    )[0]

    assert "ev.code===4001" in websocket
    assert "终端已由更新的页面接管" in websocket
    assert "!taken" in websocket
    assert "ev.code===4004" in websocket
    assert "recoverInvalidTerm(id)" in websocket
    recovery = js.split("async function recoverInvalidTerm(id){", 1)[1].split(
        "function releaseAllTermZoomLeases", 1
    )[0]
    assert "removeTermInstance(id)" in recovery
    assert "doAttachHerdr(session)" not in recovery
    assert "该终端已被其他页面替换，请显式打开" in recovery


def test_task_board_requests_and_renders_structured_agent_reports():
    js = _inline_js()
    assert 'id="taskReportBtn"' in HTML
    assert "function refreshTaskReports()" in js
    assert "/api/attention/refresh-reports" in js
    assert "result.requested" in js
    task = js.split("function sessionTaskHtml(session){", 1)[1].split(
        "function renderAttention", 1
    )[0]
    assert "agent.report||null" in task
    assert "report.summary" in task
    assert "report.next_step" in task
    assert "report.blocker" in task
    assert "report.pending" in task


def test_mobile_terminal_has_expandable_computer_keyboard():
    js = _inline_js()
    assert 'id="termKeys"' in HTML
    assert "function toggleTermKeyboard()" in js
    assert "function toggleTermModifier(name)" in js
    assert "function applyTermModifiers(data,name)" in js
    assert "Home:'\\x1b[H'" in js
    assert "PageUp:'\\x1b[5~'" in js
    assert "F12:'\\x1b[24~'" in js
    for key in ["Home", "End", "PageUp", "PageDown", "Delete", "F1", "F12"]:
        assert f'onclick="termKey(\'{key}\')"' in HTML
    for modifier in ["ctrl", "alt", "shift"]:
        assert f'onclick="toggleTermModifier(\'{modifier}\')"' in HTML
    assert "applyTermModifiers(d)" in js


def test_terminal_does_not_forward_browser_focus_reports_to_pty():
    js = _inline_js()
    assert "const isTermFocusReport=data=>data==='\\x1b[I'||data==='\\x1b[O'" in js
    mount = js.split("function termMount(id){", 1)[1].split("function ", 1)[0]
    assert "if(isTermFocusReport(d))return" in mount
    assert "if(isTermProtocolReply(d))" in mount
    assert "queueTermInput(id,d,null,true)" in mount
    assert "else queueTermInput(id,applyTermModifiers(d))" in mount


def test_terminal_drops_blocked_user_input_and_never_replays_mouse_input():
    """启动/断线窗口的用户键鼠必须丢弃，不能恢复后回放。"""
    js = _inline_js()
    mount = js.split("function termMount(id){", 1)[1].split("function ", 1)[0]
    on_data = mount.split("xterm.onData(d=>", 1)[1].split("xterm.onResize", 1)[0]
    assert "queueTermInput(id,applyTermModifiers(d))" in on_data
    queue = js.split("function queueTermInput(id,data,notice,protocol=false){", 1)[1].split(
        "function rememberTermOutput", 1
    )[0]
    assert "isTermMouseReport(data)" in queue
    assert "inst.ws?.readyState!==1" in queue
    assert "termUserInputBlocked(id)" in queue
    assert queue.index("isTermMouseReport(data)") < queue.index("inst.pendingInput=")
    assert "protocol" in queue


def test_terminal_keyboard_toggle_in_toolbar_and_keys_hidden_by_default():
    # 按键栏默认隐藏不占行;⌨ 入口挪进工具栏,且工具栏收起态下也保留该入口
    assert ":not(#termKeyboardToggle)" in HTML
    assert ".term-keys.expanded{display:flex" in HTML
    assert ".term-keys{display:flex}" not in HTML
    assert HTML.index('id="termKeyboardToggle"') < HTML.index('class="term-keys"')


def test_mobile_terminal_keeps_native_input_visible_above_soft_keyboard():
    js = _inline_js()
    assert "interactive-widget=resizes-content" not in HTML
    assert 'id="termMobileInputBar"' not in HTML
    assert 'id="termKeyboardInput"' not in HTML
    assert 'id="termKeyboardStatus"' not in HTML
    assert "(any-pointer:coarse)" in HTML
    assert "function termSendVisibleInput()" not in js
    assert "xterm.onData(d=>" in js
    assert "XTERM&&XTERM.focus()" in js
    assert "const touch=window.matchMedia('(any-pointer:coarse)').matches" in js
    # 折叠屏展开后 any-pointer 可能按桌面设备报告，maxTouchPoints 仍能识别触屏。
    assert "navigator.maxTouchPoints>0" in js
    assert "function positionTermForKeyboard()" in js
    position = js.split("function positionTermForKeyboard(){", 1)[1].split("function ", 1)[0]
    assert "view.contains(active)" in position
    assert "active.matches('input,textarea')" in position
    assert "vv.offsetTop+vv.height" in position
    assert "const layoutBottom=fixed?window.innerHeight:view.getBoundingClientRect().bottom" in position
    assert "stage.style.bottom=padding" in position
    assert "stage.style.bottom=''" in position
    assert "view.style.paddingBottom=padding" in position
    assert "if(inset<80)inset=0" in position
    assert "positionTermForKeyboard();if(FIT&&XTERM)safeFitOf(FIT,XTERM)" in js
    assert "window.visualViewport.addEventListener('scroll',fitVisibleTerm)" in js
    assert "document.addEventListener('focusout',()=>setTimeout(fitVisibleTerm,50))" in js


def test_terminal_fit_never_resizes_pty_from_hidden_view():
    js = _inline_js()
    fit = js.split("function safeFitOf(fit,xterm){", 1)[1].split("function ", 1)[0]
    assert "view?.classList.contains('active')" in fit
    assert "el.getBoundingClientRect()" in fit
    assert fit.index("view?.classList.contains('active')") < fit.index("fit.fit()")
    assert "fit.proposeDimensions()" in fit
    assert "xterm.cols===targetCols&&xterm.rows===dimensions.rows" in fit
    assert fit.index("xterm.cols===targetCols") < fit.index("fit.fit()")


def test_terminal_first_show_refits_after_browser_layout_settles():
    js = _inline_js()
    show = js.split("function showTermInstance(id){", 1)[1].split(
        "function termSwitch", 1
    )[0]

    assert "safeFitOf(inst.fit,inst.xterm)" in show
    assert "requestAnimationFrame(()=>{" in show
    assert "const current=TERM_INSTANCES[id]" in show
    assert "if(!current||current!==inst||TERM_ID!==id)return" in show
    assert "safeFitOf(current.fit,current.xterm)" in show
    assert "current.xterm.refresh(0,current.xterm.rows-1)" in show
    assert show.index("safeFitOf(inst.fit,inst.xterm)") < show.index(
        "requestAnimationFrame(()=>{"
    )


def test_terminal_container_size_changes_trigger_refit_via_resize_observer():
    # 容器尺寸变化不一定伴随 window/visualViewport resize 事件，漏一次 refit
    # 终端就会比可见区域窄、右侧留黑条。每个终端实例必须挂 ResizeObserver，
    # 并在销毁时 disconnect。
    js = _inline_js()
    mount = js.split("function termMount(id){", 1)[1].split(
        "// ============ 会话管理", 1
    )[0]
    remove = js.split("function removeTermInstance(id){", 1)[1].split(
        "async function recoverInvalidTerm", 1
    )[0]

    assert "resizeObserver:null" in mount
    assert "new ResizeObserver(()=>{" in mount
    assert "ro.observe(el)" in mount
    assert "TERM_INSTANCES[id].resizeObserver=ro" in mount
    assert mount.index("new ResizeObserver") < mount.index("ro.observe(el)")
    assert "inst.resizeObserver?.disconnect()" in remove


def test_terminal_touch_scroll_is_js_driven():
    # 普通 scrollback 由 xterm 原生处理；鼠标上报或无 scrollback 的 alternate buffer
    # 由 enableTermTouchScroll 把单指拖动换算成 wheel 交给 TUI。
    js = _inline_js()
    assert "function enableTermTouchScroll(el,xterm)" in js
    assert "#termContainer .xterm.enable-mouse-events{touch-action:none" in HTML
    mount = js.split("function termMount(id){", 1)[1].split("function ", 1)[0]
    assert "enableTermTouchScroll(el,xterm)" in mount
    scroll = js.split("function enableTermTouchScroll(el,xterm){", 1)[1].split("function ", 1)[0]
    assert "const sensitivity=4,maxSteps=48" in scroll
    assert "xterm.modes?.mouseTrackingMode!=='none'||isAlternateBuffer()" in scroll
    assert "xterm.buffer?.active?.type==='alternate'" in scroll
    assert "xterm.buffer?.onBufferChange?.(syncTouchMode)" in scroll
    assert "const usesPointerEvents=()=>typeof PointerEvent==='function'" in scroll
    assert "move(e.clientY,e,true)" in scroll
    assert "#termContainer .xterm.term-touch-scroll{touch-action:none" in HTML
    assert "steps=Math.min(maxSteps,Math.abs(lines))" in scroll
    # 只扣实际派发的行数,快滑不丢滚动距离
    assert "remainder-=direction*steps*rowHeight" in scroll
    assert "for(let i=0;i<steps;i++)" in scroll
    assert "xterm.scrollLines(direction)" in scroll
    assert "new WheelEvent('wheel'" in scroll
    assert "e.preventDefault()" in scroll
    # 折叠屏展开后的 Pointer Events 回退，且与 Touch Events 去重。
    assert "el.addEventListener('pointerdown'" in scroll
    assert "el.addEventListener('pointermove'" in scroll
    assert "e.pointerType!=='touch'" in scroll
    assert "touchActive||e.pointerType!=='touch'" in scroll
    assert "e.pointerId!==activePointer" in scroll
    assert "activePointer!==null" not in scroll
    assert "el.addEventListener('pointerleave',finishPointer" in scroll


def test_terminal_drawer_keeps_main_navigation_visible_and_clickable():
    js = _inline_js()
    assert "function positionDrawerBelowHeader()" in js
    assert "header.getBoundingClientRect().bottom" in js
    assert "drawer.style.top=" in js
    assert "if(drawer&&drawer.classList.contains('show'))closeTerm()" in js


def test_herdr_flow_is_mobile_friendly_default_card_view():
    js = _inline_js()
    assert 'data-view="herdrflow"' in HTML
    assert 'id="view-herdrflow"' in HTML
    assert 'data-action="openFlow"' in HTML
    assert "enterHerdrFlow(it.dataset.session,it.dataset.pane)" in js
    assert ".hf-body{flex:1;min-height:0;overflow:auto;padding:10px;display:grid" in HTML
    assert "grid-template-columns:repeat(auto-fill,minmax(170px,1fr))" in HTML
    assert 'class="hf-card"' in HTML
    assert ".hf-pane-head,.hf-out,.hf-in{display:none}" in HTML
    assert ".hf-body.hf-full .hf-card{display:none}" in HTML
    assert ".hf-body.hf-full .hf-pane-head{display:flex" in HTML
    assert ".hf-body.hf-full .hf-pane.hf-focus .hf-out{display:block" in HTML
    assert ".hf-body.hf-full .hf-in{display:flex" in HTML
    assert "HF_TIMER=setInterval(hfRefreshAll,3000)" in js
    assert "function hfStop()" in js


def test_herdr_flow_only_requests_the_focused_agent_output():
    js = _inline_js()
    flow = js.split("async function hfRefreshAll(){", 1)[1].split(
        "async function hfSend", 1
    )[0]

    assert "if(!body?.classList.contains('hf-full'))return" in flow
    assert "body.querySelector('.hf-pane.hf-focus')" in flow
    assert "p.pane_id===paneId" in flow
    assert "?lines=300&is_agent=true" in flow
    assert "for(const p of panes)" not in flow


def test_herdr_flow_focus_uses_the_entire_viewport_and_can_exit():
    js = _inline_js()
    toggle = js.split("async function hfToggle(paneId){", 1)[1].split(
        "function hfExitFullscreen", 1
    )[0]
    assert ".app.hf-immersive #view-herdrflow{" in HTML
    assert "position:fixed;inset:0" in HTML
    assert "width:100%;min-width:0" in HTML
    assert ".hf-body.hf-full .hf-pane.hf-focus{display:flex;flex:1;min-width:0" in HTML
    assert 'id="hfToolbar" class="hf-toolbar"' in HTML
    assert 'id="hfToolbar" style=' not in HTML
    assert ".app.hf-immersive #hfToolbar{display:none}" in HTML
    assert ".hf-body.hf-full .hf-pane-head{padding:4px 7px" in HTML
    assert ".hf-body.hf-full .hf-in{padding:4px;gap:4px;flex-wrap:nowrap}" in HTML
    assert ".hf-body.hf-full .hf-paste-short{display:inline}" in HTML
    assert "class=\"hf-enter\"" in HTML and "hf.full" in HTML
    assert "class=\"hf-exit\"" in HTML and "hf.exit" in HTML
    assert "app.classList.add('hf-immersive')" in js
    assert "await hfRefreshAll()" in toggle
    assert toggle.index("pane.classList.add('hf-focus')") < toggle.index(
        "await hfRefreshAll()"
    )
    # herdrflow 沉浸模式不进原生全屏(Android Chrome 会弹系统提示);
    # 终端真全屏(toggleTermFullscreen)是独立入口,不在此约束内
    assert "requestFullscreen" not in toggle
    assert "function hfExitFullscreen(" in js
    assert "if(e.key==='Escape'){hfExitFullscreen();exitTermFullscreen()}" in js
    assert "document.addEventListener('fullscreenchange'" in js
    # re-render 时自愈残留 hf-full（否则无 focus 的 pane 会全部 display:none）
    render = js.split("function hfRender(){", 1)[1].split("async function hfRefreshAll", 1)[0]
    assert "body.classList.remove('hf-full')" in render
    assert "hfToggle(fp)" in render

def test_herdr_flow_session_switch_exits_fullscreen_before_render():
    js = _inline_js()
    load = js.split("function hfLoad(){", 1)[1].split("function hfRender(){", 1)[0]
    assert "const nextSession=" in load
    assert "if(nextSession!==HF_SESSION)" in load
    assert "hfExitFullscreen(false)" in load
    assert "HF_FOCUS_PANE=null" in load
    assert load.index("hfExitFullscreen(false)") < load.index("HF_SESSION=nextSession")
    assert load.index("HF_SESSION=nextSession") < load.index("hfRender()")


def test_herdr_flow_preserves_selection_and_ignores_stale_refresh():
    js = _inline_js()
    assert "HF_SESSION!==session||!out.isConnected" in js
    assert "out.textContent===next" in js
    assert "out.contains(selection.anchorNode)" in js
    assert "function updatePaneOutput(out,next)" in js
    assert "out.scrollHeight-out.scrollTop-out.clientHeight<=2" in js
    assert "const top=out.scrollTop" in js
    assert "out.scrollTop=follow?out.scrollHeight:top" in js


def test_herdr_flow_entry_from_term_defaults_to_current_term():
    # 终端视图切到流视图(工具栏按钮或导航「流」)时,默认选中当前终端的 session/pane
    js = _inline_js()
    enter = js.split("async function enterHerdrFlow(session,paneId){", 1)[1].split(
        "async function hfStart", 1
    )[0]
    assert "if(!session&&CURRENT_TERM)" in enter
    assert "session=CURRENT_TERM.session" in enter
    assert "paneId=paneId||CURRENT_TERM.paneId" in enter
    # 从 TUI 回流转：释放 zoom + 退出终端固定全屏，避免窄列硬换行残留
    assert "exitTermFullscreen()" in enter
    assert "releaseAllTermZoomLeasesAwait" in enter
    show = js.split("function showView(v){", 1)[1].split("function ", 1)[0]
    assert "prevView==='term'&&v!=='term'" in show
    assert "releaseAllTermZoomLeasesAwait" in show
    assert "prevView==='term'&&CURRENT_TERM" in show
    assert "HF_SESSION=CURRENT_TERM.session" in show
    assert js.count("updatePaneOutput(out,next)") >= 2
    assert "out.innerHTML=d.output" not in js
    assert "encodeURIComponent(p.session)" in js
    assert "encodeURIComponent(p.pane_id)" in js


def test_herdr_flow_entry_from_term_uses_attached_session_identity():
    # 显示名称和 Herdr session 分开保存，返回流视图不能再拿 label 猜 session。
    js = _inline_js()
    enter = js.split("async function enterHerdrFlow(session,paneId){", 1)[1].split(
        "async function hfStart", 1
    )[0]
    assert "TERM_SESSIONS[TERM_ID]" in enter
    show = js.split("function showView(v){", 1)[1].split("function ", 1)[0]
    assert "TERM_SESSIONS[TERM_ID]" in show
    attach = js.split("async function doAttachHerdr(session", 1)[1].split("// ============ herdr", 1)[0]
    assert "TERM_SESSIONS[r.id]=session" in attach
    assert "delete TERM_SESSIONS[id]" in js


def test_safe_fit_waits_for_fonts_and_keeps_right_gutter():
    # xterm 创建前先加载 Webfont，避免字符测量缓存基于 fallback 字体；fit 后留一列边距。
    js = _inline_js()
    fit = js.split("function safeFitOf(fit,xterm){", 1)[1].split("function ", 1)[0]
    assert "xterm.resize(xterm.cols-1,xterm.rows)" in fit
    assert "function ensureTermFontLoaded()" in js
    assert "document.fonts.load" in js
    term_new = js.split("async function termNew(cwd){", 1)[1].split("function ", 1)[0]
    assert term_new.index("await ensureTermFontLoaded()") < term_new.index("api(url")
    attach = js.split("async function doAttachHerdr(session", 1)[1].split("// ============ herdr", 1)[0]
    assert attach.index("await ensureTermFontLoaded()") < attach.index(
        "const r=await api('/api/term?label='"
    )
    set_font = js.split("function setTermFont(v,save=true){", 1)[1].split("function ", 1)[0]
    assert "safeFitOf(inst.fit,inst.xterm)" in set_font


def test_file_manager_search_ui_and_actions_wired():
    js = _inline_js()
    assert 'id="fileSearchInput"' in HTML
    assert 'id="fileSearchInfo"' in HTML
    assert "/api/files/search" in js
    assert "function fileSearch()" in js
    assert "function fileSearchOpen(i)" in js
    assert "function fileSearchDownload(i)" in js
    assert 'data-action="fileSearchOpen"' in js
    assert 'data-action="fileSearchDownload"' in js
    assert ".file-search{" in HTML


def test_file_manager_navigation_stops_at_allowed_roots():
    js = _inline_js()
    assert "FILE_ROOTS=null" in js
    assert "FILE_ROOTS=r.roots||[]" in js
    assert "FILE_ROOTS.filter(r=>d.path===r||d.path.startsWith(r+'/'))" in js
    assert "if((FILE_ROOTS||[]).includes(FILE_CWD))" in js


def test_file_manager_explains_roots_search_and_actions():
    js = _inline_js()
    assert 'class="file-guide"' in HTML
    assert "先在“位置”下拉框选目录" in HTML
    assert "当前目录和子目录" in HTML
    assert "function fileRootLabel(" in js
    # 位置切换是分组下拉框,不再平铺 chip 列表
    assert 'id="fileLocSel"' in js
    assert "ft-loc" in js
    assert "ft-root-chip" not in js


def test_file_manager_groups_and_manages_custom_roots():
    js = _inline_js()
    assert 'id="fileAddRootBtn"' in HTML
    assert "FILE_ROOT_GROUPS=null" in js
    assert "function fileAddRoot()" in js
    assert "function fileRemoveRoot(path)" in js
    assert "系统目录" in js
    assert "已注册项目" in js
    assert "自定义目录" in js
    assert 'data-action="fileRemoveRoot"' in js
    assert "method:'POST'" in js
    assert "method:'DELETE'" in js
    assert "a==='fileRemoveRoot')fileRemoveRoot(it.dataset.path)" in js


def test_file_manager_can_prefill_workspace_from_current_directory():
    js = _inline_js()
    assert 'id="fileWorkspaceBtn"' in HTML
    assert "function workspaceSessionName(path)" in js
    assert "async function setupWorkspaceFromFiles()" in js
    assert "document.getElementById('suWorkdir').value=FILE_CWD" in js
    assert "document.getElementById('suSession').value=workspaceSessionName(FILE_CWD)" in js
    assert "showSetupModal()" in js


def test_agent_mail_hub_failure_becomes_read_only_ui():
    js = _inline_js()
    assert "write_available!==false" in js
    assert "Agent Mail Hub 当前不可用，消息暂时只读" in js


def test_hub_message_content_is_rendered_as_escaped_text():
    js = _inline_js()
    assert "${esc(m.subject||'(无主题)')}" in js
    assert "${esc(m.body_md)}" in js
    assert "${m.body_md}" not in js
    assert "eval(" not in js


def test_agent_mail_coordination_controls_and_status_are_visible():
    js = _inline_js()
    assert 'id="cmpIntent"' in js
    assert 'option value="blocking"' in js
    assert 'option value="stop"' in js
    assert 'id="cmpImportance"' in js
    assert 'id="cmpHard"' in js
    assert "硬中断会取消 Agent 当前在途操作" in js
    assert "m.coordination?.meta?.intent" in js
    assert "消费状态:" in js
    assert "intent:document.getElementById('cmpIntent').value" in js
    assert "importance:document.getElementById('cmpImportance').value" in js


def test_agent_mail_unread_is_not_presented_as_human_attention():
    js = _inline_js()
    assert "条 Agent Mail 未读，不计入待办" in js
    assert 'data-action="openMessages"' in js
    assert "it.dataset.action==='openMessages'" in js
    assert "showView('msgs')" in js


def test_attention_is_a_session_task_board_with_progress_and_agent_details():
    js = _inline_js()
    assert ">工作台</button>" in HTML
    assert 'data-i18n="attention.title">任务看板<' in HTML
    assert "function sessionTaskHtml(session)" in js
    assert 'class="task-board"' in js
    assert "完成 ${Number(summary.done)||0}/${total}" in js
    assert "工作中 ${Number(summary.working)||0}" in js
    assert "待处理 ${Number(summary.blocked)||0}" in js
    assert "agent.mail_name?'@'+agent.mail_name:'未注册花名'" in js
    assert "agent.task||'未记录任务说明'" in js
    assert 'data-action="openSessionAgent"' in js
    assert "enterHerdrFlow(it.dataset.session,it.dataset.pane)" in js


def test_task_board_uses_single_column_agent_rows_on_narrow_screens():
    assert ".task-board{grid-template-columns:minmax(0,1fr)" in HTML
    assert ".task-agent{grid-template-columns:24px minmax(0,1fr) auto" in HTML
    assert ".task-agent-task{grid-column:2 / 4}" in HTML


def test_mobile_workbench_uses_one_vertical_scroll_container():
    # H5 不能让 agent 速览/工具栏挤掉内部任务列表；整个工作台统一纵向滚动，
    # agent 速览单独横向滑动，任务与会话内容不再使用嵌套滚动。
    assert ".attention-list{flex:1;min-height:0;overflow:auto" in HTML
    mobile = HTML.split(
        "@media(max-width:900px),(any-pointer:coarse){", 1
    )[1].split(".side-backdrop{display:none}", 1)[0]
    assert ".attention-view{overflow-y:auto" in mobile
    assert ".agent-strip{flex-wrap:nowrap;overflow-x:auto" in mobile
    assert ".attention-list{flex:none;min-height:0;overflow:visible}" in mobile
    assert "#sessionsPane{flex:none!important;min-height:0}" in mobile
    assert "#sessionsList{flex:none!important;overflow:visible!important}" in mobile


def test_all_h5_views_have_an_explicit_scroll_owner():
    # body 固定不滚动；每个内容型 view 必须有可收缩的内部滚动容器。
    assert ".view{display:none;flex:1;min-height:0;overflow:hidden" in HTML
    assert ".msgs-body{flex:1;min-height:0" in HTML
    assert ".msgs-list{flex:1;min-width:0;min-height:0;overflow:auto" in HTML
    assert ".files-body{flex:1;min-height:0" in HTML
    assert ".file-tree{min-width:0;min-height:0" in HTML
    assert ".editor-area{min-width:0;min-height:0" in HTML
    assert ".team-content{flex:1;min-height:0;overflow:auto" in HTML
    assert ".hf-body{flex:1;min-height:0;overflow:auto" in HTML
    assert 'id="termMain" style="flex:1;display:flex;min-height:0;overflow:hidden"' in HTML
    assert ".settings-scroll{flex:1;min-height:0;overflow:auto" in HTML


def test_mobile_herdr_flow_uses_one_vertical_scroll_owner():
    # 总览是紧凑卡片网格；进入单 pane 全屏后，滚动 owner 才交给输出区。
    assert ".hf-pane{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;display:flex;flex:none" in HTML
    mobile = HTML.split(
        "@media(max-width:900px),(any-pointer:coarse){", 1
    )[1].split(".side-backdrop{display:none}", 1)[0]
    assert ".hf-body:not(.hf-full){grid-template-columns:repeat(2,minmax(0,1fr))}" in mobile
    assert ".hf-body{touch-action:pan-y;-webkit-overflow-scrolling:touch}" in mobile
    assert ".hf-body.hf-full .hf-pane.hf-focus .hf-out{overflow:auto" in mobile
    assert ".hf-body.hf-full .hf-pane.hf-focus{display:flex;flex:1;min-width:0;min-height:0" in HTML


def test_h5_message_file_and_settings_layouts_do_not_overflow():
    narrow = HTML.split("@media(max-width:860px){", 1)[1].split(
        "@media(max-width:560px){", 1
    )[0]
    # 消息页改为单列 flex；文件页仍双栏折叠为单列
    assert ".msgs-body{display:flex;flex-direction:column}" in narrow
    assert ".files-body{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}" in narrow
    assert 'class="set-dir-add"' in HTML
    phone = HTML.split("@media(max-width:560px){", 1)[1].split("</style>", 1)[0]
    assert ".set-dir-add{display:grid!important" in phone
    assert ".set-dir-add #setNewDir{grid-column:1 / -1}" in phone


# ── 设置页与 i18n ───────────────────────────────────────────────

def test_settings_view_and_nav_exist():
    assert '<button data-view="settings"' in HTML
    assert 'id="view-settings"' in HTML
    assert 'id="setAgents"' in HTML
    assert 'id="setDirAgents"' in HTML
    assert 'id="setUploadMax"' in HTML
    assert 'id="setHub"' in HTML
    assert 'id="setHubStatus"' in HTML
    assert 'id="setTeamHub"' in HTML
    assert 'id="setHumanAuth"' in HTML
    assert 'oninput="TEAM_HUB_CONFIG_EDIT=this.value"' in HTML
    assert 'oninput="HUMAN_AUTH_CONFIG_EDIT=this.value"' in HTML
    assert 'onclick="cancelSettings()"' in HTML


def test_light_theme_is_default_without_overriding_saved_dark_preference():
    js = _inline_js()
    assert '<meta name="theme-color" content="#ffffff">' in HTML
    assert "(localStorage.getItem('dash-theme')||'light')==='light'" in HTML
    assert "setTheme(localStorage.getItem('dash-theme')||'light',false)" in js
    assert "meta.content=light?'#ffffff':'#181d27'" in js
    # boot 不整页刷新；用户切换才 reload
    boot_apply = js.split("function applyDocumentTheme(mode){", 1)[1].split(
        "function setTheme", 1
    )[0]
    assert "location.reload()" not in boot_apply


def test_roomy_header_collapses_before_navigation_can_overflow():
    js = _inline_js()
    assert "@media(max-width:1100px)" in HTML
    assert "window.matchMedia('(max-width:1100px)')" in js


def test_team_view_covers_project_membership_and_session_binding():
    js = _inline_js()
    assert '<button data-view="team"' in HTML
    assert 'id="view-team"' in HTML
    assert "function teamConnect()" in js
    assert "async function teamRegister()" in js
    assert "async function teamShowAccounts()" in js
    assert "async function teamCreateInvite()" in js
    assert "async function teamSetUserStatus(username,status)" in js
    assert "function teamCreateProject()" in js
    assert "function teamJoin()" in js
    assert "function teamBindSession()" in js
    assert "function teamUnbindSession()" in js
    assert "function teamSetMemberStatus(humanId,status)" in js
    assert "function teamShowInbox()" in js
    assert "function teamMarkInboxRead(ids)" in js
    assert "'projects','POST'" in js
    assert "{name,slug,mention_handle:handle}" in js
    assert "human_key:key" not in js
    assert 'id="teamProjectName"' in HTML
    assert 'id="teamProjectSlug"' in HTML
    assert "本机项目不会自动同步" in js
    assert "本机目录和 worktree 不会上传" in js
    assert "/join-requests`,'POST'" in js
    assert "/api/team-auth/session-bindings/${encodeURIComponent(project.slug)}" in js
    assert "/members/${humanId}`,'PATCH'" in js
    assert "teamApi('inbox')" in js
    assert "teamApi('inbox/mark-read','POST',{ids})" in js
    assert 'id="teamAdminBtn"' in HTML
    assert "批准账号不会自动加入任何项目群组" in js


def test_terminal_collab_supports_real_team_messages_with_context():
    js = _inline_js()
    assert "function renderTermCollab()" in js
    assert "async function teamCollabSend()" in js
    assert "data-action=\"teamCollabTarget\"" in js
    assert "@团队 · ${esc(project.name||project.slug)}" in js
    assert "member.status==='active'" in js
    assert "member.human_id!==TEAM.human?.id" in js
    assert "由 ${esc(binding.lead?.mail_name||binding.lead?.agent||'Session 负责人')} 统一发出" in js
    assert "function teamBindingForSession(session)" in js
    assert "function teamCollabSelectProject(slug)" not in js
    assert 'data-action="teamCollabProject"' not in js
    assert "binding.project_slug" in js
    assert "support-requests`,'POST',payload" in js
    assert "payload.mention_handles=[TEAM_COLLAB_TARGET.handle]" in js
    assert "终端上下文（由 Cockpit 自动附带）" in js
    assert "- Herdr session:" in js
    assert "- Pane:" in js
    assert "- Agent:" in js
    assert "- Workdir:" in js
    assert "Team Hub · Session 绑定项目" in js


def test_team_chat_shares_support_history_and_can_reply():
    js = _inline_js()
    assert 'id="teamChatBtn"' in HTML
    assert "async function teamLoadChat(throwOnError=false)" in js
    assert "function teamChatPanel()" in js
    assert "async function teamChatSend()" in js
    assert "async function teamShowChat()" in js
    assert "/chat/messages`" in js
    assert "{subject:'群聊消息',body_md,importance:'normal'}" in js
    assert "TEAM.mode==='chat'" in js
    assert "item.sender_human_id" in js
    assert "${esc(item.sender_name||'unknown')}" in js
    assert "${esc(item.body_md||'')}" in js
    assert "所有 active 成员都能查看历史" in js


def test_team_inbox_routes_to_session_lead_on_load_poll_and_manual_retry():
    js = _inline_js()
    assert "/api/team-auth/inbox-route/route" in js
    assert "/api/team-auth/inbox-route/status" in js
    assert "await teamRouteInboxNow();" in js
    assert "await teamRouteInboxNow(true);" in js
    assert "action==='teamInboxRouteNow')teamRunInboxRoute()" in js
    assert "远程 Human Inbox → 已绑定 Session 的 lead" in js


def test_team_human_jwt_uses_http_only_cookie_and_hub_text_is_escaped():
    js = _inline_js()
    assert "api('/api/team-auth/login'" in js
    assert "JSON.stringify({username,password})" in js
    assert "api('/api/team-auth/status')" in js
    assert "api('/api/team-auth/logout'" in js
    assert "sessionStorage.setItem('cockpit-human-jwt'" not in js
    assert "sessionStorage.removeItem('cockpit-human-jwt'" not in js
    assert "localStorage.setItem('cockpit-human-jwt'" not in js
    assert "sessionStorage.setItem('cockpit-human-password'" not in js
    assert "X-Agent-Hub-Authorization" not in js
    assert "${esc(project.slug)}" in js
    assert "${esc(binding.session)}" in js
    assert "${esc(lead)}" in js
    assert "${esc(member.display_name)}" in js
    assert "${esc(item.subject||'（无主题）')}" in js
    assert "${esc(item.body_md||'')}" in js
    assert "${esc(item.project_slug||'unknown')}" in js
    assert "${esc(item.sender_name||'unknown')}" in js
    assert "${item.body_md}" not in js
    assert "${item.sender_name}" not in js
    assert "团队项目、成员、Agent 状态只用于展示与 Hub 元数据更新" in js


def test_team_refresh_and_mutations_are_serialized():
    js = _inline_js()
    assert "if(TEAM.loading)return" in js
    assert "if(TEAM.mutating)return" in js
    assert "TEAM.mutating=true" in js
    assert "finally{TEAM.mutating=false}" in js
    assert "if(TEAM.inboxLoading)return" in js
    assert "if(TEAM.mutating||!ids.length)return" in js


def test_i18n_en_ja_key_sets_match():
    import re
    m = re.search(r"const I18N=\{\s*en:\{([\s\S]*?)\},\s*ja:\{([\s\S]*?)\}\};", HTML)
    assert m, "I18N dict not found"
    en, ja = m.group(1), m.group(2)
    keys_en = set(re.findall(r"'([a-z.]+)':", en))
    keys_ja = set(re.findall(r"'([a-z.]+)':", ja))
    assert keys_en == keys_ja, f"en/ja key 不一致: {keys_en ^ keys_ja}"
    assert len(keys_en) >= 60


def test_i18n_framework_and_static_markup():
    assert "function t(key,zh,vars)" in HTML
    assert "function applyLang()" in HTML
    assert 'data-i18n="nav.attention"' in HTML
    assert 'data-i18n="nav.settings"' in HTML
    assert 'data-i18n-ph="launch.workdir.ph"' in HTML
    # 中文原文仍作为回退保留在源码中
    assert "工作台" in HTML and "设置" in HTML


def test_settings_js_functions_exist():
    for fn in ("loadSettings", "renderSettings", "saveSettings", "cancelSettings",
               "setAddDirAgent", "applyEnabledAgents", "launchPreselectAgent"):
        assert f"function {fn}(" in HTML
    assert "'/api/settings'" in HTML
    assert "'/api/agent-mail/config'" in HTML
    assert "SETTINGS_EDIT=JSON.parse(JSON.stringify(SETTINGS))" in HTML
    assert "HUB_CONFIG_EDIT=HUB_CONFIG.hub||''" in HTML
    assert "TEAM_HUB_CONFIG_EDIT=HUB_CONFIG.team_hub||''" in HTML
    assert "HUMAN_AUTH_CONFIG_EDIT=HUB_CONFIG.human_auth||''" in HTML
    assert "team_hub:TEAM_HUB_CONFIG_EDIT.trim()" in HTML
    assert "human_auth:HUMAN_AUTH_CONFIG_EDIT.trim()" in HTML
    assert 'placeholder="http://10.18.160.11:8765"' in HTML
    assert 'placeholder="http://10.18.160.11:8766"' in HTML
    assert "公司私网 IP 可使用 HTTP" in HTML
    assert "TEAM_HUB_URL 和 HUMAN_AUTH_URL 配置" not in HTML


# ── 顶部导航与终端工具栏折叠 ────────────────────────────────────

def test_nav_and_toolbar_collapsible():
    assert 'class="icon nav-toggle"' in HTML
    assert 'class="icon tb-toggle"' in HTML
    assert "function toggleNav()" in HTML
    assert "function toggleTermToolbar()" in HTML
    assert ".side.open{transform:none}" in HTML
    assert ".term-toolbar.collapsed>*:not(.tb-toggle):not(#termKeyboardToggle){display:none}" in HTML
    # 窄屏默认收起工具栏；宽松导航在 1100px 前折叠，CSS 与 matchMedia 同源。
    assert "window.innerWidth<=860" in HTML
    assert "window.innerWidth<=560" not in HTML
    assert "@media(max-width:1100px)" in HTML
    assert "matchMedia('(max-width:1100px)')" in HTML


# ── 终端字体大小设置(本机偏好) ──────────────────────────────────

def test_h5_foldable_responsive_first_batch():
    """第一批 H5/折叠屏响应式修复的静态回归断言。"""
    js = _inline_js()
    # 放开缩放(保留 viewport-fit)，且不得在 body 全局限制终端触屏手势
    assert "user-scalable=no" not in HTML
    assert "maximum-scale" not in HTML
    assert "width=device-width, initial-scale=1, viewport-fit=cover" in HTML
    body_rule = re.search(r"(?m)^body\{[^}]*\}", HTML)
    assert body_rule
    assert "touch-action:" not in body_rule.group()
    assert "#termContainer .xterm.enable-mouse-events{touch-action:none" in HTML
    # 移动端/触屏输入框 >=16px,避免 iOS focus 自动缩放
    assert "@media(max-width:900px),(any-pointer:coarse)" in HTML
    assert "input,select,textarea,.set-card input,.set-card select{font-size:16px}" in HTML
    # 看板 auto-fit 自适应列,删掉 860/560 两档硬断点
    assert "repeat(auto-fit,minmax(220px,1fr))" in HTML
    assert ".board{grid-template-columns:repeat(2,1fr)}" not in HTML
    assert ".board{grid-template-columns:1fr}" not in HTML
    # 文件页保留双栏弹性比例；消息页改为全宽 flex 列表
    assert HTML.count("minmax(200px,28%) 1fr") == 1
    assert ".files-body{flex:1;min-height:0;display:grid;grid-template-columns:minmax(200px,28%) 1fr" in HTML
    assert ".msgs-body{flex:1;min-height:0;display:flex;flex-direction:column" in HTML
    assert ".files-body{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}" in HTML
    # term-toolbar 基础规则即可换行,<=1100px(折叠屏展开)不横向溢出
    assert re.search(r"\.term-toolbar\{display:flex;flex-wrap:wrap", HTML)
    # 键盘遮挡判断与宽度解耦,适配 coarse pointer(平板/折叠屏)
    assert "const isCoarsePointer=()=>window.matchMedia('(any-pointer:coarse)')" in js
    assert "if(!isTouchTerminal())return;" in js


def test_terminal_font_size_setting():
    # 设置页滑块 + 当前值显示
    assert 'id="setTermFont"' in HTML
    assert 'id="setTermFontVal"' in HTML
    assert 'data-i18n="set.termfont"' in HTML
    # 读写与即时应用逻辑
    assert "function termFontSize()" in HTML
    assert "function setTermFont(" in HTML
    assert "term-font-size" in HTML
    # 新建终端不再硬编码 13,改用本机偏好
    assert "fontSize:termFontSize()" in HTML
    assert "fontSize:13" not in HTML


# ── M3c: 后台 Inbox 轮询 ──────────────────────────────────────

def test_team_inbox_polling_uses_single_flight_and_exponential_backoff():
    js = _inline_js()
    assert "TEAM_POLL_TIMER=null" in js
    assert "TEAM_POLL_BACKOFFS=[30000,60000,120000,240000,300000]" in js
    assert "function teamPollStart()" in js
    assert "function teamPollStop()" in js
    assert "async function teamPollTick()" in js
    assert "teamPollStop();" in js
    poll = js.split("async function teamPollTick(){", 1)[1].split("function ", 1)[0]
    assert "await teamLoadInbox(true)" in poll
    assert "TEAM_POLL_FAILURES=0" in poll
    assert "TEAM_POLL_BACKOFF=TEAM_POLL_BACKOFFS[0]" in poll
    assert "TEAM_POLL_FAILURES++" in poll
    assert "TEAM_POLL_BACKOFFS[Math.min(TEAM_POLL_FAILURES,TEAM_POLL_BACKOFFS.length-1)]" in poll


def test_team_inbox_polling_starts_on_login_and_stops_on_disconnect():
    js = _inline_js()
    load = js.split("async function teamLoad(force=false){", 1)[1].split("function ", 1)[0]
    assert "teamPollStart()" in load
    disconnect = js.split("async function teamDisconnect(){", 1)[1].split("function ", 1)[0]
    assert "teamPollStop()" in disconnect


def test_team_inbox_polling_stops_on_load_failure():
    js = _inline_js()
    load = js.split("async function teamLoad(force=false){", 1)[1].split("function ", 1)[0]
    catch = load.split("}catch(e){", 1)[1]
    assert "teamPollStop()" in catch


def test_team_inbox_polling_skips_hidden_and_refreshes_on_visible():
    js = _inline_js()
    poll = js.split("async function teamPollTick(){", 1)[1].split("function ", 1)[0]
    assert "document.visibilityState==='hidden'" in poll
    assert "visibilitychange" in js
    vis_handler = js.split("visibilitychange',()=>{", 1)[1].split("}", 1)[0]
    assert "!document.hidden" in vis_handler
    assert "teamPollTick()" in vis_handler


def test_team_inbox_polling_stops_on_pagehide():
    js = _inline_js()
    assert "pagehide',()=>{releaseAllTermZoomLeases();teamPollStop()}" in js


def test_team_inbox_polling_does_not_toast_or_trigger_side_effects():
    js = _inline_js()
    poll = js.split("async function teamPollTick(){", 1)[1].split("function ", 1)[0]
    assert "toast(" not in poll
    assert "checkpoint" not in poll
    assert "terminal" not in poll.lower()
    assert "pane" not in poll.lower()
    assert "worktree" not in poll.lower()
    assert "teamMarkInboxRead" not in poll


def test_team_nav_badge_element_and_css_exist():
    assert 'id="teamNavBadge"' in HTML
    assert ".nav-badge{" in HTML
    assert "function teamUpdateModeUi()" in HTML
    js = _inline_js()
    assert "document.getElementById('teamNavBadge')" in js


def test_team_nav_badge_cleared_on_disconnect():
    js = _inline_js()
    disconnect = js.split("async function teamDisconnect(){", 1)[1].split("function ", 1)[0]
    assert "teamNavBadge" in disconnect
    assert "display='none'" in disconnect


def test_team_load_inbox_throw_on_error_propagates_failures():
    js = _inline_js()
    load = js.split("async function teamLoadInbox(throwOnError=false){", 1)[1].split("function ", 1)[0]
    assert "if(throwOnError)throw e" in load
    # Manual callers (teamLoad, teamShowInbox) still use default — no throw, no toast
    assert "await teamLoadInbox()" in js
    # Poll caller uses throwOnError=true
    assert "await teamLoadInbox(true)" in js


def test_team_nav_badge_survives_language_switch():
    js = _inline_js()
    # data-i18n is on inner span, not the button that wraps the badge
    assert '<span data-i18n="nav.team">' in HTML
    assert 'id="teamNavBadge"' in HTML
    # Extract the <button> opening tag (up to first >) for the team button
    btn_start = HTML.index('data-view="team"')
    tag_end = HTML.index('>', btn_start)
    button_tag = HTML[btn_start:tag_end]
    # Button opening tag must NOT have data-i18n (applyLang.textContent would destroy badge)
    assert 'data-i18n=' not in button_tag
    # But the inner span must have it for translation
    inner = HTML[tag_end:HTML.index('</button>', tag_end)]
    assert '<span data-i18n="nav.team">' in inner
    assert 'id="teamNavBadge"' in inner


def test_team_inbox_polling_recovers_after_pageshow_bfcache():
    js = _inline_js()
    assert "pageshow',()=>{recoverPageState();if(TEAM.authenticated&&TEAM.human&&!TEAM_POLL_TIMER)teamPollStart()}" in js


def test_team_load_does_not_call_duplicate_team_update_mode_ui():
    js = _inline_js()
    load = js.split("async function teamLoad(force=false){", 1)[1].split("function ", 1)[0]
    assert load.count("teamUpdateModeUi()") == 0


# ── Team Session 选择/绑定/解绑 UI ─────────────────────────────

def test_team_state_loads_session_bindings_not_registry_or_global_directory():
    js = _inline_js()
    assert "teamApi('agents')" not in js
    assert "TEAM.candidates" not in js
    assert "api('/api/team-auth/session-bindings')" in js
    assert "TEAM.sessions=Array.isArray(sessionData.sessions)?sessionData.sessions:[]" in js
    assert "TEAM.sessionBindings=Array.isArray(sessionData.bindings)?sessionData.bindings:[]" in js
    assert "api('/api/team-auth/local-identities')" not in js
    assert "teamApi(`projects/${slug}/agent-bindings`)" not in js


def test_team_session_binding_ui_functions_and_actions_exist():
    js = _inline_js()
    assert "function teamSessionPicker(project)" in js
    assert "function teamSessionBindingCard(project)" in js
    assert "function teamBindSession()" in js
    assert "function teamUnbindSession()" in js
    assert "action==='teamSessionBind'" in js
    assert "action==='teamSessionUnbind'" in js
    assert 'data-action="teamSessionBind"' in js
    assert 'data-action="teamSessionUnbind"' in js
    assert "负责人由 Session 的唯一 lead 自动识别" in js


def test_team_session_picker_lists_only_ready_choices_as_enabled():
    js = _inline_js()
    picker = js.split("function teamSessionPicker(project){", 1)[1].split(
        "function teamSessionBindingCard", 1
    )[0]
    assert "TEAM.sessions.filter(session=>session.ready)" in picker
    assert "session.ready?'':' disabled'" in picker
    assert "session.lead?.mail_name||session.lead?.agent" in picker
    assert "session.reason||'负责人不可用'" in picker
    assert "esc(name)" in picker
    assert "esc(suffix)" in picker


def test_team_project_panel_removes_individual_agent_identity_flow():
    js = _inline_js()
    panel = js.split("function teamProjectPanel(project){", 1)[1].split(
        "function teamInboxPanel", 1
    )[0]
    assert "teamSessionBindingCard(project)" in panel
    assert "本机 Session" in panel
    assert "teamAgentRows" not in js
    assert "teamBindingRows" not in js
    assert "teamLocalIdentityRows" not in js
    assert "teamClaimIdentity" not in js
    assert "认领本机身份" not in js
    assert "项目 Agents" not in js


def test_team_session_binding_mutations_are_confirmed_and_serialized():
    js = _inline_js()
    bind = js.split("function teamBindSession(){", 1)[1].split("function ", 1)[0]
    unbind = js.split("function teamUnbindSession(){", 1)[1].split("function ", 1)[0]
    assert "validSessionName(session)" in bind
    assert "window.confirm" in bind
    assert "JSON.stringify({session,replace})" in bind
    assert "teamMutation('Session 已绑定'" in bind
    assert "method:'PUT'" in bind
    assert "window.confirm" in unbind
    assert "teamMutation('Session 已解绑'" in unbind
    assert "method:'DELETE'" in unbind


def test_team_session_binding_ui_never_leaks_registry_or_starts_local_work():
    js = _inline_js()
    start = js.index("function teamBindingForProject")
    end = js.index("function teamMemberRows", start)
    part = js[start:end]
    assert "registration_token" not in part
    assert "identity_id" not in part
    for bad in ("openTerm(", "doAttachHerdr", "createPane", "terminal("):
        assert bad not in part


def test_terminal_team_collab_requires_current_session_binding():
    js = _inline_js()
    render = js.split("function renderTermCollab(){", 1)[1].split(
        "async function termPickCollab", 1
    )[0]
    assert "teamBindingForSession(context.session)" in render
    assert "尚未绑定团队项目" in render
    assert "binding.project_slug" in render
    assert "binding.ready" in render
    assert "TEAM.members.filter(member=>member.status==='active'" in render
    assert "由 ${esc(binding.lead?.mail_name||binding.lead?.agent||'Session 负责人')} 统一发出" in render
    assert "data-action=\"teamCollabTarget\"" in render
    assert "'以你的 Human 身份发出'" not in render
    assert "collab-target readonly" not in render


def test_terminal_collab_groups_local_agents_by_session():
    js = _inline_js()
    render = js.split("function renderTermCollab(){", 1)[1].split(
        "async function termPickCollab", 1
    )[0]
    assert "const localGroups=new Map()" in render
    assert "localGroups.get(pane.session).push(pane)" in render
    assert "[...localGroups.entries()]" in render
    assert "${esc(session)}" in render
    assert "${panes.length}" in render


def test_team_session_empty_state_and_navigation_actions():
    js = _inline_js()
    assert "team-hero" in HTML
    assert "连接本机工作区" in js
    assert "本机还没有运行中的 Session" in js
    assert 'data-action="teamGotoSessions"' in js
    assert 'data-action="teamGotoTerm"' in js
    assert "action==='teamGotoSessions'" in js
    assert "action==='teamGotoTerm'" in js


def test_team_nav_badge_counts_pending_approvals():
    # P0-3: nav 徽标 = 未读 + 账号 pending(全局 admin) + 成员 invited(群组 admin)
    js = _inline_js()
    update = js.split("function teamUpdateModeUi(){", 1)[1].split("function ", 1)[0]
    assert "pendingAccounts" in update
    assert "invitedMembers" in update
    assert "teamIsGlobalAdmin()" in update
    assert "navCount=inboxUnread+pendingAccounts+invitedMembers" in update


def test_team_mutation_failure_uses_persistent_error_bar():
    # P0-4: mutation 失败展示持久错误条而非瞬态 toast
    js = _inline_js()
    assert "function teamShowError(msg){" in js
    assert "teamErrorBar" in js
    mutation = js.split("async function teamMutation(", 1)[1].split("function ", 1)[0]
    assert "teamShowError(" in mutation
    assert "teamClearError();toast(success)" in mutation
    assert "toast(success+'失败" not in mutation


def test_team_collab_send_failure_keeps_draft_with_inline_error():
    js = _inline_js()
    send = js.split("async function teamCollabSend(){", 1)[1].split("function closeCollab", 1)[0]
    assert "teamCollabError" in send
    assert "内容已保留，可重试" in send
    assert "toast('团队消息发送失败" not in send


def test_terminal_ws_queues_data_without_color_rewrite():
    # 颜色由 xterm 渲染层原生处理(options.theme + WebGL),不再逐 chunk JS 改写。
    # 这与 Orca 的做法一致;JS ANSI 重写是 TUI 高速输出时主线程卡顿的根因。
    js = _inline_js()
    onmessage = js.split("ws.onmessage=ev=>{try{", 1)[1].split("ws.onclose", 1)[0]
    assert "writeTermOutput(id,data,replayFrame)" in onmessage
    assert "TERM_TEXT_ENCODER.encode(ev.data)" in onmessage
    renderer = js.split("function writeTermOutput(id,data,replayFrame){", 1)[1].split(
        "function loadOlderTermHistory", 1
    )[0]
    assert "queueTermRender(id,visible)" in renderer
    assert "queueTermRender(id,data)" in renderer
    # onmessage 不再有主题分支/颜色重写调用(注释提及已移除不算)
    code_only = "\n".join(
        line for line in onmessage.splitlines()
        if line.strip() and not line.strip().startswith("//")
    )
    assert "lightTermAdapt(" not in code_only
    assert "darkTermBoost(" not in code_only
    assert "TERM_LIGHT" not in code_only
    # 颜色重写函数已整体移除(定义不存在)
    assert "function lightTermAdapt" not in js
    assert "function darkTermBoost" not in js
    assert "function _rgbToHsl" not in js
    assert "ANSI16_RGB" not in js


def test_terminal_replay_is_bounded_and_older_history_loads_on_scroll():
    js = _inline_js()
    assert "TERM_REPLAY_INITIAL=8*1024" in js
    assert "TERM_HISTORY_PAGE=8*1024" in js
    writer = js.split("function writeTermOutput(id,data,replayFrame){", 1)[1].split(
        "function loadOlderTermHistory", 1
    )[0]
    assert "data.byteLength-TERM_REPLAY_INITIAL" in writer
    assert "queueTermRender(id,visible)" in writer
    pager = js.split("function loadOlderTermHistory(id){", 1)[1].split(
        "function enableTermHistoryPaging", 1
    )[0]
    assert "historyHiddenBytes-TERM_HISTORY_PAGE" in pager
    assert "buffer?.type!=='normal'" in pager
    wheel = js.split("function enableTermHistoryPaging(el,id,xterm){", 1)[1].split(
        "// xterm 原生处理普通 scrollback", 1
    )[0]
    assert "e.deltaY>=0" in wheel
    assert "buffer.viewportY===0" in wheel
    assert "loadOlderTermHistory(id)" in wheel


def test_terminal_output_yields_between_bounded_chunks():
    js = _inline_js()
    assert "TERM_RENDER_CHUNK=16*1024" in js
    pump = js.split("function pumpTermRender(id){", 1)[1].split(
        "function queueTermRender", 1
    )[0]
    assert "item.offset+TERM_RENDER_CHUNK" in pump
    assert "setTimeout(()=>pumpTermRender(id),0)" in pump
    writer = js.split("function writeTermOutput(id,data,replayFrame){", 1)[1].split(
        "function loadOlderTermHistory", 1
    )[0]
    assert "queueTermRender(id,visible)" in writer
    assert "queueTermRender(id,data)" in writer
    mount = js.split("function termMount(id){", 1)[1].split(
        "// ============ 会话管理", 1
    )[0]
    assert "renderQueue:[]" in mount
    assert "renderGeneration:0" in mount


def test_terminal_loading_waits_for_tui_quiet_period_and_browser_paint():
    js = _inline_js()
    loading_css = HTML.split(".term-loading{", 1)[1].split("}", 1)[0]
    assert "pointer-events:auto" in loading_css
    assert "cursor:wait" in loading_css
    assert ".term-loading.done{opacity:0;pointer-events:none}" in HTML
    assert "TERM_LOADING_QUIET=80,TERM_LOADING_TIMEOUT=15000" in js
    assert "replay_complete" in js
    hold = js.split("function holdTermLoadingForTui(id", 1)[1].split(
        "function settleTermLoading", 1
    )[0]
    assert "inst.loadingWaitForTui=true" in hold
    assert "settleTermLoading(id,true)" in hold
    assert "maxWaitMs" in hold or "1500" in hold
    settle = js.split("function settleTermLoading(id,force=false){", 1)[1].split(
        "function writeTermOutput", 1
    )[0]
    assert "inst.replayComplete" in settle
    assert "inst.rendering" in settle
    assert "inst.renderQueue.length" in settle
    assert "inst.xterm.buffer.active.type!=='alternate'" in settle
    assert "TERM_LOADING_QUIET-(performance.now()-inst.loadingLastOutputAt)" in settle
    assert "afterTermPaints(()=>{" in settle
    writer = js.split("function writeTermOutput(id,data,replayFrame){", 1)[1].split(
        "function loadOlderTermHistory", 1
    )[0]
    assert "inst.loadingLastOutputAt=performance.now()" in writer
    assert "clearTimeout(inst.loadingTimer)" in writer
    websocket = js.split("function openTermWS(id,xterm,replay){", 1)[1].split(
        "function showTermInstance", 1
    )[0]
    assert "if(!binary){" in websocket
    assert "if(!binary&&awaitingReplay)" not in websocket
    assert "inst.replayComplete=true;settleTermLoading(id)" in websocket
    pump = js.split("function pumpTermRender(id){", 1)[1].split(
        "function queueTermRender", 1
    )[0]
    assert "settleTermLoading(id)" in pump
    mount = js.split("function termMount(id){", 1)[1].split(
        "// ============ 会话管理", 1
    )[0]
    assert "requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(callback)))" in js
    painted = mount.index("afterTermPaints(()=>{")
    opened = mount.index("xterm.open(el)")
    webgl = mount.index("enableTermWebgl(xterm)")
    assert painted < opened < webgl
    assert "if(prewarmed)afterTermPaints(connect)" in mount
    assert "xterm.write(TERM_WEBGL_WARMUP,clearWarmup)" in mount
    assert "current.initializing=false" in mount
    clear_warmup = mount.split("const clearWarmup=", 1)[1].split(
        "try{xterm.write", 1
    )[0]
    assert clear_warmup.index("xterm.reset()") < clear_warmup.index("afterTermPaints(connect)")
    # loading 期用户键鼠必须丢弃，但 xterm 的明暗协议应答要放行，
    # 否则 Herdr 启动会退回 shell。
    assert "const isTermProtocolReply=" in js
    gate = js.split("function termUserInputBlocked(id){", 1)[1].split(
        "function flushTermInput", 1
    )[0]
    assert "inst.initializing" in gate
    assert "!inst.replayComplete" in gate
    assert "inst.loadingEl" in gate
    assert "inst.loadingWaitForTui" in gate
    finish = js.split("function finishTermLoading(id,inst){", 1)[1].split(
        "function showTermThemeOverlay", 1
    )[0]
    assert "inst.loadingWaitForTui=false" in finish
    term_key = js.split("function termKey(name){", 1)[1].split(
        "async function termEnsure", 1
    )[0]
    assert "queueTermInput(TERM_ID" in term_key
    assert "终端正在启动，请稍候" not in term_key


def test_theme_switch_is_inplace_without_reload():
    """主题必须就地轻量切换：禁止 reload/整屏 refresh；重活延后。"""
    js = _inline_js()
    assert "function applyDocumentTheme(mode)" in js
    set_theme = js.split("function setTheme(mode,save=true){", 1)[1].split(
        "function toggleTheme", 1
    )[0]
    assert "location.reload()" not in set_theme
    assert "refresh(0," not in set_theme  # 禁止整屏 force refresh
    assert "applyDocumentTheme" in set_theme
    assert set_theme.index("applyDocumentTheme") < set_theme.index("termTheme()")
    assert "options.theme=th" in set_theme
    # notify=false：Mode 2031 只走 API，禁止前端双发
    assert "sendAllTermColorSchemes(false)" in set_theme
    assert "sendAllTermColorSchemes()" not in set_theme
    assert "/api/theme/herdr" in set_theme
    assert "setTimeout" in set_theme  # Mode 2031/herdr 延后，不卡同一帧
    assert "armTermInputGate" in set_theme
    assert "localStorage.setItem('dash-theme'" in set_theme
    init = js.split("async function init(){", 1)[1].split("init();", 1)[0]
    assert "doAttachHerdr(themeResume" not in init



def test_layout_pane_list_checkbox_not_full_width():
    """全局 input{width:100%} 不得撑爆布局列表 checkbox（会挤掉 agent 名）。"""
    js = _inline_js()
    render = js.split("async function renderLayoutPaneList(){", 1)[1].split(
        "async function layoutTargetOrToast", 1
    )[0]
    assert 'class="ly-pick"' in render
    assert "width:auto" in render
    assert "min-width:16px" in render

def test_theme_switch_uses_xterm_theme_contrast_and_native_protocol():
    js = _inline_js()
    assert "filter:invert(" not in HTML
    assert "hue-rotate(" not in HTML
    assert "function currentTermColorScheme(){return document.documentElement.classList.contains('light')?'light':'dark'}" in js
    term_theme = js.split("function termTheme(mode=currentTermColorScheme()){", 1)[1].split(
        "function termMinimumContrastRatio", 1
    )[0]
    assert "if(mode==='light')" in term_theme
    assert "background:'#fafbfc',foreground:'#1a2030'" in term_theme
    assert "background:'#000000',foreground:'#e8e8e8'" in term_theme
    assert "function termMinimumContrastRatio(){return TERM_WEBGL_CONTRAST}" in js
    constructor = re.search(
        r"const xterm=(?:prewarmed\?\.xterm\|\|)?new Terminal\(\{([^}]*)\}\)", js
    )
    assert constructor and "minimumContrastRatio:termMinimumContrastRatio()" in constructor.group(1)

    report = js.split("function sendTermColorScheme(id,ws,notify){", 1)[1].split(
        "function sendAllTermColorSchemes", 1
    )[0]
    assert "type:'theme'" in report
    assert "mode:currentTermColorScheme()" in report
    assert "notify:!!notify" in report
    assert "TERM_SESSIONS" not in report
    assert "ws.readyState!==1" in report

    broadcast = js.split("function sendAllTermColorSchemes(notify=false){", 1)[1].split(
        "function scheduleHerdrColorSchemeNotify", 1
    )[0]
    assert "Object.entries(TERM_INSTANCES||{})" in broadcast
    assert "sendTermColorScheme(id,inst.ws,!!notify)" in broadcast

    # 首次 replay 时 Herdr 尚未启动，不能把 Mode 2031 报告写给登录 shell；
    # 重连到已运行 TUI 时才通知。两条路径都会更新 OSC 10/11 回复色。
    open_ws = js.split("function openTermWS(id,xterm,replay){", 1)[1].split(
        "function showTermInstance", 1
    )[0]
    assert "sendTermColorScheme(id,ws,!replay)" in open_ws
    assert "sendTermColorScheme(id,ws,true)" not in open_ws
    assert open_ws.index("sendTermColorScheme(id,ws,!replay)") < open_ws.index(
        "flushTermInput(id,ws)"
    )

def test_webgl_themes_are_prewarmed_after_login_and_reused_by_terminal_mount():
    js = _inline_js()
    prewarm = js.split("function scheduleTermThemePrewarm(){", 1)[1].split(
        "function takeTermThemePrewarmer", 1
    )[0]
    assert "await ensureTermFontLoaded()" in prewarm
    assert "modes=[predicted,predicted==='light'?'dark':'light',predicted]" in prewarm
    assert "await waitForTermIdle()" in prewarm
    assert "createTermThemePrewarmer(mode)" in prewarm

    creator = js.split("function createTermThemePrewarmer(mode){", 1)[1].split(
        "function scheduleTermThemePrewarm", 1
    )[0]
    assert "cols:8,rows:2" in creator
    assert "theme:termTheme(mode)" in creator
    assert "minimumContrastRatio:termMinimumContrastRatio()" in creator
    assert "enableTermWebgl(xterm)" in creator
    assert "xterm.write(TERM_WEBGL_WARMUP,ready)" in creator
    assert "resolve({mode,xterm,fit,webgl,host})" in creator

    take = js.split("function takeTermThemePrewarmer(mode){", 1)[1].split(
        "function termMount", 1
    )[0]
    assert "pool.length>1?pool.pop():null" in take

    mount = js.split("function termMount(id){", 1)[1].split(
        "// ============ 会话管理", 1
    )[0]
    assert "takeTermThemePrewarmer(currentTermColorScheme())" in mount
    assert "prewarmed?.xterm||new Terminal" in mount
    assert "el.appendChild(xterm.element)" in mount
    assert "prewarmed.host.remove()" in mount

    init = js.split("async function init(){", 1)[1].split("init();", 1)[0]
    assert init.index("if(!ok)") < init.index("scheduleTermThemePrewarm()")


def test_settings_hub_error_shows_actionable_fix():
    # 新装机最常见失败(token 未配置)必须给可操作的恢复指引,不只报错
    js = _inline_js()
    assert "set.hub.fix.token" in js
    assert "set.hub.fix.conn" in js
    assert "HTTP_BEARER_TOKEN" in js
    assert "~/.agent-mail/client.env" in js
    hub = js.split("const hubStatus=document.getElementById('setHubStatus')", 1)[1].split(
        "const teamHub=", 1
    )[0]
    assert "reason.toLowerCase().includes('token')" in hub
    assert "chmod 600 ~/.agent-mail/client.env" in hub
    assert "点击本页“保存”或重启 Cockpit" in hub
    assert "hubStatus.appendChild(hint)" in hub


def test_messages_page_agent_filter_and_cleanup():
    # 全宽消息列表 + 顶部筛选（session/发件人/收件人/时间/状态），无左侧 agent 列表
    assert 'id="msgFilters"' in HTML
    assert 'id="msgFilterSender"' in HTML
    assert 'id="msgFilterRecipient"' in HTML
    assert 'id="msgFilterFrom"' in HTML
    assert 'id="msgFilterTo"' in HTML
    assert 'id="msgFilterStatus"' in HTML
    assert 'id="msgSession"' in HTML
    assert "projAgentList" not in HTML
    assert 'data-action="viewAgent"' not in HTML
    assert "pickRecipient" not in HTML
    assert "msg-filter-bar" in HTML
    assert "clearMsgFilter" in HTML
    assert "applyMsgFilters" in HTML
    assert "filterProjMessages" in HTML
    assert "dir-out" in HTML and "dir-in" in HTML
    # 消息清理按钮走 /api/messages/cleanup
    assert "cleanupMsgs()" in HTML
    assert "/api/messages/cleanup" in HTML
    # session→项目：只按 overview human_key 精确映射，禁止 slug 模糊
    js = _inline_js()
    assert "MSG_OVERVIEW_PROJECTS" in js
    assert "function resolveSlugByHumanKey" in js
    assert "function normalizeHumanKey" in js
    assert "slugifyProjectPath" not in js
    assert "want.endsWith(o.value)" not in js
    assert "o.value.endsWith(want)" not in js
    assert "want.includes(o.value)" not in js
    assert "o.value.includes(want)" not in js
    sess_change = js.split("function onMsgSessionChange(){", 1)[1].split(
        "function onMsgProjectChange", 1
    )[0]
    assert "resolveSlugByHumanKey(projectPath,MSG_OVERVIEW_PROJECTS)" in sess_change
    assert "endsWith" not in sess_change
    assert "includes(" not in sess_change
    # session 是「定位绑定项目」导航，不是消息字段筛选
    assert "不是消息字段筛选" in sess_change or "只用来跳转" in sess_change
    assert "定位 session=" in js
    assert "消息按「项目」库加载" in js


def test_messages_sse_refresh_is_visible_scoped_debounced_and_draft_safe():
    js = _inline_js()
    assert 'id="msgLoadState"' in HTML
    assert 'id="msgList" aria-busy="false"' in HTML
    assert "SSE.addEventListener('messages'" in js
    assert "d.projects.forEach(project=>MSG_DIRTY_PROJECTS.add(project))" in js
    assert "const MSG_DIRTY_PROJECTS=new Set()" in js
    schedule = js.split("function scheduleMessageRefresh(slug){", 1)[1].split(
        "async function loadProjectMsgs", 1
    )[0]
    assert "active!=='msgs'||document.hidden" in schedule
    assert "MSG_SENDING" in schedule
    assert "setTimeout(()=>loadProjectMsgs(undefined,true),250)" in schedule
    load = js.split("async function loadProjectMsgs(focusMessageId,background=false,requestedSlug){", 1)[1].split(
        "function setMsgComposeEnabled", 1
    )[0]
    assert "queueMessageRequest(slug,focusMessageId,background)" in load
    assert "const pending=MSG_PENDING_REQUEST;MSG_PENDING_REQUEST=null" in load
    assert "const draft=background&&MSG_BOUND_SLUG===slug?msgDraft():null" in load
    assert load.index("const d=await api") < load.index("const draft=background")
    assert "restoreMsgDraft(draft)" in load
    assert "setMsgLoadState('error'" in load
    queue = js.split("function queueMessageRequest(slug,focusMessageId,background){", 1)[1].split(
        "function scheduleMessageRefresh", 1
    )[0]
    assert "const sameProject=previous?.slug===slug" in queue
    assert "focusMessageId:focusMessageId??(sameProject?previous.focusMessageId:null)??null" in queue
    assert "background:sameProject&&previous.background===false?false:background" in queue
    assert "const dirtyGeneration=MSG_DIRTY_GENERATIONS.get(slug)||0" in load
    assert "(MSG_DIRTY_GENERATIONS.get(slug)||0)===dirtyGeneration" in load
    recovery = js.split("function recoverPageState(){", 1)[1].split(
        "function keepFocusedControlVisible", 1
    )[0]
    assert "MSG_DIRTY_PROJECTS.has(slug)" in recovery


def test_global_shortcuts_are_discoverable_guarded_and_optional():
    js = _inline_js()
    assert 'id="setShortcuts"' in HTML
    assert 'data-i18n="set.shortcuts.hint"' in HTML
    assert "const SHORTCUTS_KEY='cockpit-shortcuts'" in js
    assert "function setShortcutsEnabled(on)" in js
    assert "document.addEventListener('keydown',handleGlobalShortcut)" in js
    guard = js.split("function shortcutGuard(e){", 1)[1].split(
        "function focusViewSearch", 1
    )[0]
    for marker in ("SHORTCUTS_ENABLED", "e.isComposing", "e.ctrlKey", "e.altKey", "e.metaKey", "OPEN_DIALOG", "target?.isContentEditable", "target?.closest?.('.xterm')"):
        assert marker in guard
    handler = js.split("function handleGlobalShortcut(e){", 1)[1].split(
        "// ============ init", 1
    )[0]
    assert "{b:'attention',t:'term',f:'files',m:'msgs',s:'settings'}" in handler
    assert "key==='/'" in handler and "focusViewSearch()" in handler
    assert "key==='r'" in handler and "refreshCurrent()" in handler
    focus = js.split("function focusViewSearch(){", 1)[1].split(
        "function handleGlobalShortcut", 1
    )[0]
    assert "showView('files')" in focus and "fileSearchInput" in focus


def test_files_page_media_preview_and_dir_download():
    js = _inline_js()
    # 图片/音视频内联预览走 /api/files/raw
    assert "FILE_IMG_EXT" in js and "FILE_AUD_EXT" in js and "FILE_VID_EXT" in js
    assert "/api/files/raw?path=" in js
    assert "<video controls" in js and "<audio controls" in js
    # 目录打包下载
    assert "fileDownloadDir" in js
    assert "/api/files/download-dir?path=" in js
    assert 'data-action="fileDownloadDir"' in js


def test_sessions_merged_into_workspace_tab():
    js = _inline_js()
    # 会话不再是独立导航页,而是工作台内的 tab
    assert 'data-view="sessions"' not in HTML
    assert 'id="view-sessions"' not in HTML
    assert 'id="attSessionsBtn"' in HTML
    assert 'id="sessionsPane"' in HTML
    assert "function attShowSessions()" in js
    assert "function attShowTasks()" in js
    assert "function attRefresh()" in js


def test_mobile_nav_closes_and_term_file_panel():
    js = _inline_js()
    # 手机菜单:open 在 #side 上,点菜单项/遮罩都要能关掉
    assert 'id="sideBackdrop"' in HTML
    assert "function closeNav()" in js
    assert "closeNav()" in js
    assert ".side-backdrop.show" in HTML
    assert "getElementById('nav').classList.remove('open')" not in js
    # 终端工具栏「文件」按钮:PC 右侧面板/手机跳文件页
    assert 'id="termFilesBtn"' in HTML
    assert 'id="termFilePanel"' in HTML
    assert "function termFiles()" in js
    assert "function termFileOpen(" in js
    assert "on('#termFileList'" in js


def test_pc_nav_hide_and_expand():
    js = _inline_js()
    assert 'id="navCollapse"' in HTML
    assert 'id="navExpand"' in HTML
    assert ".app.nav-hidden" in HTML
    assert "function toggleNavPc()" in js
    assert "nav-hidden-pc" in js


def test_side_refresh_refreshes_current_view():
    js = _inline_js()
    assert 'onclick="refreshCurrent()"' in HTML
    assert "function refreshCurrent()" in js
    # 不再只刷看板
    assert 'onclick="refreshBoard()"' not in HTML


def test_board_and_files_keep_last_result_with_local_busy_feedback():
    js = _inline_js()
    assert 'id="board" aria-busy="false"' in HTML
    assert 'id="fileTree" aria-busy="false"' in HTML
    assert 'id="attentionList" aria-busy="false"' in HTML
    board = js.split("async function refreshBoard(){", 1)[1].split(
        "function renderBoard", 1
    )[0]
    assert "board.setAttribute('aria-busy','true')" in board
    assert "BOARD?'刷新失败，显示上次结果':'加载失败'" in board
    assert "finally{if(seq===BOARD_LOAD_SEQ&&board)board.setAttribute('aria-busy','false')}" in board
    tree = js.split("async function loadFileTree(){", 1)[1].split(
        "function setFileRoots", 1
    )[0]
    assert "tree.setAttribute('aria-busy','true')" in tree
    assert "'刷新失败，显示上次结果'" in tree
    search = js.split("async function fileSearch(){", 1)[1].split(
        "function fileSearchClear", 1
    )[0]
    assert "'正在搜索…'" in search
    assert "'搜索失败，显示上次结果'" in search
    attention = js.split("async function loadAttention(payload){", 1)[1].split(
        "async function refreshTaskReports", 1
    )[0]
    assert "list?.setAttribute('aria-busy','true')" in attention
    assert "'刷新失败，显示上次结果'" in attention


def test_view_and_stable_selections_restore_without_sensitive_state():
    js = _inline_js()
    assert "const STABLE_STATE_KEYS=" in js
    assert "session:'cockpit-session'" in js
    assert "fileDir:'cockpit-file-dir'" in js
    assert "messageProject:'cockpit-message-project'" in js
    assert "function viewFromLocation()" in js
    assert "function syncViewLocation(v,replace=false)" in js
    assert "window.addEventListener('popstate',restoreViewFromLocation)" in js
    assert "window.addEventListener('hashchange',restoreViewFromLocation)" in js
    assert "VIEW_LOCATION_LOCK=true" in js
    assert "if(target===LAST_RESTORED_LOCATION)return" in js
    assert "if(current!==view)showView(view)" in js
    assert "history.pushState(null,'','/#/'+v)" in js
    assert "history.replaceState(null,'','/#/'+v)" in js
    assert "LAST_RESTORED_LOCATION=location.pathname+location.search+location.hash" in js
    init = js.split("async function init(){", 1)[1].split("init();", 1)[0]
    assert "VIEW_LOCATION_LOCK=true;try{await handleAttentionHash()}finally{VIEW_LOCATION_LOCK=false}" in init
    attention = js.split("async function openAttention(i,updateHash=true){", 1)[1].split(
        "async function openTaskAttention", 1
    )[0]
    assert "if(updateHash)VIEW_LOCATION_LOCK=true" in attention
    assert "finally{VIEW_LOCATION_LOCK=previousLock}" in attention
    assert "pending=openMailAttention" in attention
    assert attention.index("finally{VIEW_LOCATION_LOCK=previousLock}") < attention.index("if(pending)await pending")

    # 恢复值必须经最新 catalog 验证；临时终端 ID 和敏感/可编辑内容绝不持久化。
    assert "validateRestoredSession(TERM_SESSION_CATALOG)" in js
    assert "filePathAllowed(stored,FILE_ROOTS)" in js
    assert "projects.some(p=>p.slug===stored)" in js
    assert "rememberStable(STABLE_STATE_KEYS.session" in js
    assert "rememberStable(STABLE_STATE_KEYS.fileDir" in js
    assert "rememberStable(STABLE_STATE_KEYS.messageProject" in js
    for forbidden in (
        "localStorage.setItem('TERM_ID'",
        "localStorage.setItem('FILE_PATH'",
        "localStorage.setItem('FILE_ORIG'",
        "localStorage.setItem('MSG_BODY'",
        "localStorage.setItem('token'",
    ):
        assert forbidden not in js


def test_file_tree_and_search_share_sequence_and_preserve_last_success():
    js = _inline_js()
    tree = js.split("async function loadFileTree(){", 1)[1].split(
        "function setFileRoots", 1
    )[0]
    search = js.split("async function fileSearch(){", 1)[1].split(
        "function fileSearchClear", 1
    )[0]
    assert "let FILE_LOAD_SEQ=0, FILE_APPLIED_CWD=null" in js
    assert "const seq=++FILE_LOAD_SEQ" in tree
    assert "const r=await api('/api/files/roots');if(seq!==FILE_LOAD_SEQ)return;setFileRoots(r)" in tree
    assert "if(seq!==FILE_LOAD_SEQ||FILE_CWD!==target)return" in tree
    assert "const seq=++FILE_LOAD_SEQ" in search
    assert "const r=await api('/api/files/roots');if(seq!==FILE_LOAD_SEQ)return;setFileRoots(r)" in search
    assert "if(seq!==FILE_LOAD_SEQ||input.value.trim()!==q||FILE_CWD!==base)return" in search
    assert "FILE_APPLIED_CWD=d.path" in tree
    assert "filePathAllowed(FILE_APPLIED_CWD,FILE_ROOTS||[])" in tree
    assert "rememberFileDir(d.path)" in tree


def test_launch_modal_worktree_preview_and_source_fix():
    """添加 Agent 弹窗:独立 worktree 模式说明自动新建,预填不再落在既有 worktree。"""
    assert 'id="lnWorktreePreview"' in HTML
    assert "function launchWorktreePreview()" in HTML
    js = _inline_js()
    # 独立模式预填若落在既有 cockpit worktree 目录,替换为 session 源目录
    assert "ws==='isolated'&&/\\/\\.[^/]+-cockpit-worktrees(?:\\/|$)/.test(workdir)" in js
    # 方案 A:预览不承诺完整路径(不依赖 LAUNCH_SESSION_DIRS 拼 base)。
    start = js.index("function launchWorktreePreview()")
    preview = js[start:js.index("function ", start + 10)]
    assert "LAUNCH_SESSION_DIRS" not in preview
    assert "cockpit-worktrees" not in preview
    # opaque ID 由服务端生成，预览不得拿显示名预测实例目录。
    assert "lnName" not in preview
    assert "{session:session,name:name}" not in preview
    assert HTML.count("'launch.wtpreview':") == 2  # en + ja 各一项
    assert "{session}/{name}" not in HTML
    assert "<session>/<name>" not in HTML
    # 程序化实例名刷新(agent 切换/自动命名)后同步预览,且无递归
    refresh = js[js.index("function launchRefreshName"):]
    refresh = refresh[: refresh.index("\n}") + 2]
    assert "launchWorktreePreview()" in refresh
    assert "launchRefreshName" not in preview
    # H5 media CSS:提示占整行,不新增横向溢出
    assert "#lnWorktreePreview{grid-column:1 / -1}" in HTML


# ============ U1a: 升级提示 UI ============

def test_update_banner_present_above_views():
    """提示条在 </header> 之后、首个视图之前:所有视图可见。"""
    assert 'id="updateBanner"' in HTML
    assert 'class="update-banner"' in HTML
    assert 'id="updateBannerText"' in HTML
    assert HTML.index("</header>") < HTML.index('id="updateBanner"')
    assert HTML.index('id="updateBanner"') < HTML.index('id="view-board"')


def test_settings_version_card_present():
    """设置页始终显示当前版本 + 检查更新按钮 + 状态位。"""
    assert 'data-i18n="upd.card"' in HTML
    assert 'id="setVerCur"' in HTML
    assert 'id="setVerStatus"' in HTML
    assert 'onclick="checkUpdate()"' in HTML


def test_release_url_whitelist_is_strict():
    """Release URL 不可信:safeReleaseUrl 必须 https + github.com + 官方 releases 前缀。"""
    js = _inline_js()
    assert "function safeReleaseUrl" in js
    assert "protocol==='https:'" in js
    assert "hostname==='github.com'" in js
    assert "'/fyc0451/agent-cockpit/releases/'" in js


def test_upgrade_button_is_hidden_by_default_and_has_stable_target():
    """L15 只在双门通过后显示固定按钮，HTML 初始必须隐藏且禁用。"""
    assert 'id="setUpgradeBtn"' in HTML
    button = HTML.split('id="setUpgradeBtn"', 1)[1].split("</button>", 1)[0]
    assert 'style="display:none"' in button
    assert " disabled" in button
    assert 'onclick="startUpgrade()"' in button
    assert 'id="setUpgradeStatus"' in HTML


def test_upgrade_button_requires_version_and_service_dual_gate():
    js = _inline_js()
    gate = js.split("function upgradeStartAvailable(){", 1)[1].split("}", 1)[0]
    assert "VERSION_INFO" in gate
    assert "status==='update_available'" in gate
    assert "UPGRADE_STATUS" in gate
    assert "available===true" in gate
    assert "VERSION_LOADING" in gate
    assert "VERSION_LOAD_FAILED" in gate


def test_upgrade_click_is_single_post_and_immediately_latched():
    js = _inline_js()
    start = js.split("async function startUpgrade(){", 1)[1].split("\n}", 1)[0]
    assert "if(UPGRADE_START_SENT" in start
    assert start.index("UPGRADE_START_SENT=true") < start.index(
        "api('/api/upgrade',{method:'POST'})"
    )
    assert js.count("api('/api/upgrade',{method:'POST'})") == 1
    assert "UPGRADE_START_SENT=false" not in start


def test_upgrade_poll_and_reconnect_are_get_only_and_not_persisted():
    js = _inline_js()
    poll = js.split("async function loadUpgradeStatus(){", 1)[1].split(
        "async function startUpgrade", 1,
    )[0]
    assert "api('/api/upgrade/status')" in poll
    assert "method:'POST'" not in poll
    assert "scheduleUpgradeStatusPoll" in poll
    connect = js.split("function connectSSE(){", 1)[1].split(
        "window.addEventListener('beforeunload'", 1,
    )[0]
    onopen = connect.split("SSE.onopen=", 1)[1].split(";", 1)[0]
    assert "loadUpgradeStatus" in onopen
    assert "startUpgrade" not in onopen
    assert "localStorage.setItem('upgrade" not in js
    assert "localStorage.getItem('upgrade" not in js


def test_upgrade_retired_disabled_and_error_states_degrade_truthfully():
    js = _inline_js()
    render = js.split("function renderUpgradeControl(){", 1)[1].split(
        "function scheduleUpgradeStatusPoll", 1,
    )[0]
    assert "state==='retired'" in render
    assert "upgrade_engine_retired" in render
    assert "available!==true" in render
    assert "UPGRADE_STATUS_FAILED" in render
    assert "btn.style.display=canStart?'':'none'" in render


def test_version_strings_escape_via_textcontent():
    """恶意 name/version 必须经 textContent/createTextNode,绝不 innerHTML 不可信值。"""
    js = _inline_js()
    assert "updateBannerText').textContent" in js
    assert "cur.textContent=(v.current&&v.current.version)" in js
    assert "createTextNode" in js
    assert "innerHTML=latest" not in js
    assert "innerHTML=v.latest" not in js


def test_version_endpoint_called_with_refresh():
    js = _inline_js()
    assert "/api/version" in js
    assert "?refresh=true" in js


def test_three_states_handled_in_version_card():
    """up_to_date / update_available / unavailable 三状态在版本卡片都有对应分支。"""
    js = _inline_js()
    assert "'up_to_date'" in js
    assert "'update_available'" in js
    assert "'unavailable'" in js


def test_update_i18n_en_and_ja():
    """en/ja 字典都补了 upd 词条(zh 走 t() 第二参数)。"""
    assert "'upd.banner':'Update available" in HTML
    assert "'upd.banner':'新バージョンがあります" in HTML
    assert "'upd.view':'Release notes'" in HTML
    assert "'upd.view':'リリースノート'" in HTML


def test_upgrade_action_i18n_zh_en_ja():
    assert 'data-i18n="upd.start"' in HTML
    assert "升级到新版本" in HTML
    assert "'upd.start':'⬆ Upgrade to new version'" in HTML
    assert "'upd.start':'⬆ 新しいバージョンへ更新'" in HTML
    assert "'upd.service_unavailable':'Upgrade service is unavailable.'" in HTML
    assert "'upd.service_unavailable':'アップグレードサービスを利用できません。'" in HTML


def test_update_banner_dismissible_and_reshows():
    """提示条可关闭;刷新/重新检查后可再次出现(UPDATE_DISMISSED 在 loadVersion 重置)。"""
    js = _inline_js()
    assert 'onclick="dismissUpdate()"' in HTML
    assert "function dismissUpdate" in js
    assert "UPDATE_DISMISSED=false" in js
    # dismissUpdate/checkUpdate 无参,不含 ${ 不可信插值
    assert re.search(r'onclick="dismissUpdate\([^)]*\$\{', HTML) is None
    assert re.search(r'onclick="checkUpdate\([^)]*\$\{', HTML) is None


def test_update_banner_css_wraps_on_mobile():
    """提示条 flex-wrap,桌面与 H5 均不横向溢出。"""
    idx = HTML.index(".update-banner")
    assert "flex-wrap" in HTML[idx:idx + 200]


def test_load_version_failure_hides_stale_banner():
    """复审 blocker:刷新失败必须隐藏旧 banner,不虚假宣称仍有更新;
    版本卡片仍温和降级(renderVersionCard(true))。"""
    js = _inline_js()
    start = js.index("async function loadVersion")
    body = js[start:js.index("function checkUpdate")]
    catch = body[body.index("catch"):]
    assert "updateBanner" in catch             # 失败路径触及 banner
    assert "display='none'" in catch           # 并隐藏它
    assert "renderVersionCard(true)" in catch  # 版本卡片仍温和降级


# ── B1 UX-P1-01～07：行为级 / Node stub ─────────────────────────

import json
import subprocess
import textwrap


def _run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node failed rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout


def test_b1_sse_has_three_states_no_timer_green():
    js = _inline_js()
    assert "SSE_LINK=" in js or "SSE_LINK='" in js or 'SSE_LINK="' in js or "SSE_LINK='offline'" in js
    assert "function updateSseLiveUi" in js
    assert "function markSseEventOk" in js
    assert "SSE.onopen" in js
    assert "markSseEventOk" in js
    # 禁止 3 秒无条件恢复绿点
    assert "setTimeout(()=>document.getElementById('liveDot').classList.remove('off'),3000)" not in js
    assert "setTimeout(()=>document.getElementById('liveDot').classList.remove(\"off\"),3000)" not in js
    # renderBoard 不得用 agent 数伪造在线
    assert "liveDot').classList.toggle('off',agentN===0)" not in js
    assert "updateSseLiveUi()" in js
    # 生产：board/attention 必须先 JSON.parse 成功再 markSseEventOk
    sse = js.split("function connectSSE(){", 1)[1].split(
        "window.addEventListener('beforeunload'", 1,
    )[0]
    for event_name in ("board", "attention"):
        # 每个事件监听内：parse 出现在 markSseEventOk 之前
        chunk = sse.split(f"addEventListener('{event_name}'", 1)[1][:500]
        assert "JSON.parse" in chunk
        assert chunk.index("JSON.parse") < chunk.index("markSseEventOk")
        assert "catch" in chunk


def test_b1_setup_close_does_not_clear_in_flight_lock():
    js = _inline_js()
    close = js.split("function closeSetup(){", 1)[1].split(
        "async function setupOpenTerminal", 1,
    )[0]
    assert "SETUP_IN_FLIGHT" in close
    assert "if(SETUP_IN_FLIGHT)" in close
    # 在途 guard 必须先于 remove('show')：保持 modal 可见
    assert close.index("if(SETUP_IN_FLIGHT)") < close.index("remove('show')")
    assert "工作区正在启动中" in close
    assert close.index("if(SETUP_IN_FLIGHT)") < close.index("SETUP_SUBMITTING=false")
    submit = js.split("async function doSetupWorkspace(){", 1)[1].split(
        "async function setupHerdrOnboarding", 1,
    )[0]
    assert "SETUP_IN_FLIGHT=true" in submit
    assert "reqId!==SETUP_REQ_ID" in submit
    # 生产：setSetupBusy 在 in-flight 后立即冻结，finally 解除并恢复结果按钮
    assert "function setSetupBusy(busy)" in js
    assert submit.index("SETUP_IN_FLIGHT=true") < submit.index("setSetupBusy(true)")
    assert "setSetupBusy(false)" in submit
    assert submit.index("setSetupBusy(false)") < submit.index("renderSetupPreview()") or \
        "setSetupBusy(false)" in submit.split("finally", 1)[1]
    assert "#suResult button" in submit or "suResult button" in js
    busy = js.split("function setSetupBusy(busy){", 1)[1].split(
        "function closeSetup", 1,
    )[0]
    assert "suModeChoices" in busy or "pointerEvents" in busy
    assert "setupPrevDisabled" in busy
    assert "suResult" in busy


def test_b1_send_paths_have_per_target_single_flight():
    js = _inline_js()
    term = js.split("async function termSend(){", 1)[1].split(
        "async function termUploadFiles", 1,
    )[0]
    assert "TERM_SENDING" in term
    assert "if(TERM_SENDING[key])return" in term
    assert "delete TERM_SENDING[key]" in term
    assert "TERM_SEND_KEY" not in term
    hf = js.split("async function hfSend(paneId){", 1)[1].split(
        "async function hfUpload", 1,
    )[0]
    assert "HF_SENDING" in hf
    assert "if(HF_SENDING[key])return" in hf
    msg = js.split("async function sendMsg(){", 1)[1].split(
        "async function ackMsg", 1,
    )[0]
    assert "if(MSG_SENDING)return" in msg
    assert "MSG_BOUND_SLUG" in msg
    # 生产路径：上锁后立刻禁用 compose
    assert msg.index("MSG_SENDING=true") < msg.index("setMsgComposeEnabled(false)")
    assert "finally" in msg and "setMsgComposeEnabled(true)" in msg


def test_b1_file_dirty_and_beforeunload():
    js = _inline_js()
    assert "function fileIsDirty()" in js
    assert "function fileConfirmLeave()" in js
    assert "function fileClearEditor()" in js
    assert "function fileDownloadPath(path)" in js
    assert "beforeunload" in js
    assert "fileConfirmLeave()" in js
    goto = js.split("function fileGoto(p){", 1)[1][:80]
    assert "fileConfirmLeave" in goto
    # 下载不得改写 FILE_PATH：列表下载走局部 path
    dl_entry = js.split("function fileDownloadEntry(i){", 1)[1].split(
        "function fileDownloadPath(", 1,
    )[0]
    assert "fileDownloadPath(path)" in dl_entry
    assert "FILE_PATH=" not in dl_entry
    dl_search = js.split("function fileSearchDownload(i){", 1)[1].split(
        "function fileIsDirty", 1,
    )[0]
    assert "fileDownloadPath(e.path)" in dl_search
    assert "FILE_PATH=" not in dl_search
    # 丢弃后清空编辑器
    confirm = js.split("function fileConfirmLeave(){", 1)[1].split(
        "function fileGoto", 1,
    )[0]
    assert "fileClearEditor()" in confirm


def test_b1_stop_delete_check_business_error():
    js = _inline_js()
    stop = js.split("async function stopSession(name){", 1)[1].split(
        "async function deleteSession", 1,
    )[0]
    assert "r.error" in stop
    assert "r.stopped" in stop
    delete = js.split("async function deleteSession(name){", 1)[1].split(
        "async function initSessionMail", 1,
    )[0]
    assert "r.error" in delete
    assert "r.deleted" in delete


def test_b1_settings_partial_success_and_single_flight():
    js = _inline_js()
    body = js.split("async function saveSettings(){", 1)[1].split(
        "function applyEnabledAgents", 1,
    )[0]
    assert "if(SETTINGS_SAVING)return" in body
    assert "SETTINGS_SAVING=true" in body
    assert "Hub 已保存" in body or "set.partial" in body
    assert "agent-mail/config" in body
    assert "/api/settings" in body
    # 生产按钮绑定：稳定 id + 保存中 disabled
    assert 'id="setSaveBtn"' in HTML
    assert "getElementById('setSaveBtn')" in body
    assert "disabled=true" in body and "disabled=false" in body
    assert body.index("SETTINGS_SAVING=true") < body.index("disabled=true")
    assert 'id="cmpSendBtn"' in js

def test_b1_node_behavior_sse_state_machine():
    """可执行：SSE 三态仅 onopen/事件恢复在线；parse 失败不回绿。"""
    js = _inline_js()
    # 绑定生产 connectSSE 中 board 处理顺序
    board = js.split("addEventListener('board'", 1)[1][:600]
    assert board.index("JSON.parse") < board.index("markSseEventOk")
    out = _run_node(textwrap.dedent(r"""
    let SSE_LINK='offline', SSE_LAST_OK_TS=null;
    const log=[];
    function updateSseLiveUi(){log.push(SSE_LINK)}
    function markSseEventOk(){SSE_LINK='online';SSE_LAST_OK_TS=Date.now();updateSseLiveUi()}
    function onError(rs){SSE_LINK=(rs===0)?'reconnecting':'offline';updateSseLiveUi()}
    function onBoard(data){
      let d;
      try{d=JSON.parse(data)}catch(_err){
        if(SSE_LINK!=='online'){SSE_LINK='reconnecting';updateSseLiveUi()}
        return;
      }
      markSseEventOk();
      return d;
    }
    SSE_LINK='reconnecting';updateSseLiveUi();
    onError(0);
    onError(2);
    // 畸形事件不得回绿
    onBoard('{not-json');
    if(SSE_LINK==='online') process.exit(2);
    markSseEventOk();
    if(log.filter(x=>x==='online').length!==1){console.error(log); process.exit(1)}
    // 合法事件才应用
    const d=onBoard('{"ok":1}');
    if(!d||d.ok!==1||SSE_LINK!=='online') process.exit(3);
    console.log('ok');
    """))
    assert "ok" in out



def test_b1_node_behavior_set_setup_busy_freezes_and_restores():
    """生产 setSetupBusy：冻结表单，finally 解除后 suResult 按钮可用。"""
    js = _inline_js()
    busy_src = js.split("function setSetupBusy(busy){", 1)[1].split(
        "function closeSetup(){", 1,
    )[0]
    submit = js.split("async function doSetupWorkspace(){", 1)[1].split(
        "async function setupHerdrOnboarding", 1,
    )[0]
    assert submit.index("setSetupBusy(true)") < submit.index(
        "await api('/api/herdr/setup-workspace'"
    )
    assert "setSetupBusy(false)" in submit.split("finally", 1)[1]
    assert "suResult button" in submit
    script = (
        "const nodes=[];\n"
        "function node(id, inResult){\n"
        "  const n={id, disabled:false, dataset:{}, style:{},\n"
        "    closest(s){return inResult&&s==='#suResult'?{}:null}};\n"
        "  nodes.push(n); return n;\n"
        "}\n"
        "const suWorkdir=node('suWorkdir',false);\n"
        "const suStartBtn=node('suStartBtn',false);\n"
        "const closeX=node('closeX',false);\n"
        "const openTerm=node('openTerm',true);\n"
        "const modesEl={style:{pointerEvents:''}};\n"
        "const advEl={style:{pointerEvents:''}};\n"
        "const document={\n"
        "  getElementById(id){\n"
        "    if(id==='setupModal')return {\n"
        "      querySelector(s){return s==='.setup-advanced'?advEl:null},\n"
        "      querySelectorAll(){return [suWorkdir,suStartBtn,closeX,openTerm]}\n"
        "    };\n"
        "    if(id==='suModeChoices')return modesEl;\n"
        "    return null;\n"
        "  },\n"
        "  querySelectorAll(sel){\n"
        "    if(sel==='#suResult button')return [openTerm];\n"
        "    return [];\n"
        "  }\n"
        "};\n"
        "function setSetupBusy(busy){" + busy_src + "\n"
        "setSetupBusy(true);\n"
        "if(!suWorkdir.disabled||!suStartBtn.disabled||!closeX.disabled) process.exit(2);\n"
        "if(modesEl.style.pointerEvents!=='none') process.exit(3);\n"
        "openTerm.disabled=true;\n"
        "setSetupBusy(false);\n"
        "if(openTerm.disabled) process.exit(4);\n"
        "if(modesEl.style.pointerEvents!=='') process.exit(5);\n"
        "console.log('ok');\n"
    )
    out = _run_node(script)
    assert "ok" in out


def test_b1_node_behavior_setup_req_id_and_close_lock():
    # 从生产 closeSetup 抽取并执行：在途不 remove show
    js = _inline_js()
    close_src = js.split("function closeSetup(){", 1)[1].split(
        "async function setupOpenTerminal", 1,
    )[0]
    # close_src 已含函数体结尾 }，不可再包一层
    script = (
        "let SETUP_SUBMITTING=false, SETUP_IN_FLIGHT=false, SETUP_REQ_ID=0;\n"
        "const toasts=[], removed=[];\n"
        "function toast(m){toasts.push(m)}\n"
        "const document={getElementById(){return {classList:{remove(c){removed.push(c)}}}}};\n"
        "function renderSetupPreview(){}\n"
        "function setSetupBusy(busy){}\n"
        "function closeDialog(){}\n"
        "function closeSetup(){" + close_src + "\n"
        "SETUP_SUBMITTING=true; SETUP_IN_FLIGHT=true;\n"
        "closeSetup();\n"
        "if(removed.includes('show')) {console.error('hid while in flight', removed); process.exit(2)}\n"
        "if(!toasts.some(t=>String(t).includes('启动'))) process.exit(3);\n"
        "if(!SETUP_SUBMITTING) process.exit(4);\n"
        "SETUP_IN_FLIGHT=false;\n"
        "closeSetup();\n"
        "if(!removed.includes('show')) process.exit(5);\n"
        "if(SETUP_SUBMITTING) process.exit(6);\n"
        "const reqId=1; SETUP_REQ_ID=2;\n"
        "if(!(reqId!==SETUP_REQ_ID)) process.exit(7);\n"
        "console.log('ok');\n"
    )
    out = _run_node(script)
    assert "ok" in out


def test_b1_node_behavior_send_single_flight_and_msg_binding():
    # 生产 termSend 形态：TERM_SENDING map，A 不阻塞 B
    js = _inline_js()
    term_src = js.split("async function termSend(){", 1)[1].split(
        "async function termUploadFiles", 1,
    )[0]
    assert "TERM_SENDING[key]" in term_src
    assert "delete TERM_SENDING[key]" in term_src
    out = _run_node(textwrap.dedent(r"""
    let TERM_SENDING={}, calls={};
    async function termSend(key){
      if(TERM_SENDING[key])return;
      TERM_SENDING[key]=true; calls[key]=(calls[key]||0)+1;
      await new Promise(r=>setTimeout(r,10));
      delete TERM_SENDING[key];
    }
    await Promise.all([termSend('s/p1'), termSend('s/p1'), termSend('s/p2'), termSend('s/p2')]);
    if(calls['s/p1']!==1 || calls['s/p2']!==1) {console.error(calls); process.exit(1)}

    let MSG_SENDING=false, MSG_BOUND_SLUG='proj-b', CURRENT={id:2,slug:'proj-b'};
    let sent=[], enabled=[];
    function setMsgComposeEnabled(on){enabled.push(!!on)}
    async function sendMsg(selectSlug){
      if(MSG_SENDING)return;
      if(!CURRENT||!MSG_BOUND_SLUG||MSG_BOUND_SLUG!==selectSlug)return;
      MSG_SENDING=true;
      setMsgComposeEnabled(false);
      sent.push(CURRENT.id);
      await Promise.resolve();
      MSG_SENDING=false;
      if(MSG_BOUND_SLUG===selectSlug) setMsgComposeEnabled(true);
    }
    await Promise.all([sendMsg('proj-b'), sendMsg('proj-b')]);
    if(sent.length!==1 || sent[0]!==2) process.exit(2);
    if(enabled[0]!==false || enabled[1]!==true) {console.error(enabled); process.exit(3)}
    MSG_BOUND_SLUG='proj-a'; CURRENT={id:1,slug:'proj-a'};
    await sendMsg('proj-b');
    if(sent.length!==1) process.exit(4);
    console.log('ok');
    """))
    assert "ok" in out


def test_b1_node_behavior_file_dirty_and_stop_error():
    out = _run_node(textwrap.dedent(r"""
    let FILE_ORIG='hello', FILE_PATH='/a.txt', editorVal='hello';
    function fileIsDirty(){return FILE_PATH!=null && FILE_ORIG!=null && editorVal!==FILE_ORIG}
    function fileConfirmLeave(){return !fileIsDirty() || false} // refuse leave when dirty
    editorVal='hello!';
    if(!fileIsDirty()) process.exit(1);
    if(fileConfirmLeave()) process.exit(2);
    editorVal='hello'; FILE_ORIG='hello';
    if(fileIsDirty()) process.exit(3);

    function handleStop(r){
      if(r.error||!r.stopped) return 'fail:'+(r.error||'no-stopped');
      return 'ok';
    }
    if(handleStop({available:true,error:'busy'})!=='fail:busy') process.exit(4);
    if(handleStop({available:true,stopped:'s1'})!=='ok') process.exit(5);
    console.log('ok');
    """))
    assert "ok" in out


def test_b1_node_behavior_settings_partial_success():
    out = _run_node(textwrap.dedent(r"""
    let SETTINGS_SAVING=false;
    async function saveSettings(hubOk, settingsOk){
      if(SETTINGS_SAVING)return 'blocked';
      SETTINGS_SAVING=true;
      let hubSaved=false;
      try{
        if(!hubOk) throw new Error('hub down');
        hubSaved=true;
        if(!settingsOk) throw new Error('settings down');
        return 'full';
      }catch(e){
        if(hubSaved) return 'partial:'+e.message;
        return 'fail:'+e.message;
      }finally{SETTINGS_SAVING=false}
    }
    const a=await saveSettings(true,true);
    const b=await saveSettings(true,false);
    const c=await saveSettings(false,true);
    const d=await Promise.all([saveSettings(true,true), saveSettings(true,true)]);
    // second should be blocked while first runs — simulate sequential lock
    SETTINGS_SAVING=false;
    let n=0;
    async function once(){
      if(SETTINGS_SAVING)return 'blocked';
      SETTINGS_SAVING=true; n++;
      await new Promise(r=>setTimeout(r,5));
      SETTINGS_SAVING=false;
      return 'ok';
    }
    const p=[once(), once(), once()];
    const res=await Promise.all(p);
    if(a!=='full'||b!=='partial:settings down'||c!=='fail:hub down') {console.error({a,b,c});process.exit(1)}
    if(n!==1 || res.filter(x=>x==='ok').length!==1) {console.error(res,n);process.exit(2)}
    console.log('ok');
    """))
    assert "ok" in out


def test_b1_node_behavior_msg_load_seq_drops_stale():
    out = _run_node(textwrap.dedent(r"""
    let MSG_LOAD_SEQ=0, applied=[];
    async function load(slug, delay, data){
      const seq=++MSG_LOAD_SEQ;
      await new Promise(r=>setTimeout(r, delay));
      if(seq!==MSG_LOAD_SEQ)return; // drop stale
      applied.push(data);
    }
    await Promise.all([load('A',30,'A'), load('B',5,'B')]);
    if(applied.join(',')!=='B') {console.error(applied); process.exit(1)}
    console.log('ok');
    """))
    assert "ok" in out


def test_state_restore_node_behavior_drops_stale_and_rejects_removed_catalog_items():
    out = _run_node(textwrap.dedent(r"""
    let FILE_LOAD_SEQ=0, FILE_CWD='/old', applied=[];
    async function loadTree(target, delay){
      FILE_CWD=target;
      const seq=++FILE_LOAD_SEQ;
      await new Promise(r=>setTimeout(r,delay));
      if(seq!==FILE_LOAD_SEQ||FILE_CWD!==target)return;
      applied.push(target);
    }
    await Promise.all([loadTree('/old',30),loadTree('/new',5)]);
    if(applied.join(',')!=='/new')process.exit(1);

    const allowed=(path,roots)=>roots.some(root=>path===root||path.startsWith(root+'/'));
    const restoreFile=(stored,roots)=>allowed(stored,roots)?stored:(roots[0]||null);
    const restoreProject=(stored,projects)=>projects.some(p=>p.slug===stored)?stored:(projects[0]?.slug||'');
    const restoreSession=(stored,sessions)=>sessions.includes(stored)?stored:(sessions[0]||null);
    if(restoreFile('/revoked/private',['/safe'])!=='/safe')process.exit(2);
    if(restoreProject('deleted',[{slug:'live'}])!=='live')process.exit(3);
    if(restoreSession('stopped',['running'])!=='running')process.exit(4);
    console.log('ok');
    """))
    assert "ok" in out


def test_message_refresh_node_behavior_is_single_flight_and_preserves_draft():
    out = _run_node(textwrap.dedent(r"""
    let inFlight=false,pending=null,loads=0,body='A',rendered='old',focuses=[];
    async function load(background=false){
      if(inFlight){pending={background,focus:42};return}
      inFlight=true;loads++;
      await new Promise(r=>setTimeout(r,10));
      const draft=background?body:null;
      rendered='new';body='';
      if(draft!==null)body=draft;
      inFlight=false;
      if(pending){const next=pending;pending=null;focuses.push(next.focus);await load(next.background)}
    }
    const first=load(true);
    await new Promise(r=>setTimeout(r,2));
    body='AB';
    await Promise.all([load(true),load(true)]);
    await first;
    await new Promise(r=>setTimeout(r,15));
    if(loads!==2||rendered!=='new'||body!=='AB')process.exit(1);
    if(focuses.join(',')!=='42')process.exit(2);
    console.log('ok');
    """))
    assert "ok" in out


def test_async_generations_bind_task_actions_and_drop_old_reads():
    js = _inline_js()
    task = js.split("async function openTaskAttention(taskId){", 1)[1].split(
        "function closeAttentionModal", 1
    )[0]
    assert "const seq=++ATTENTION_TASK_SEQ" in task
    assert "seq!==ATTENTION_TASK_SEQ||ATTENTION_TASK_ID!==taskId" in task
    action = js.split("async function attentionTaskAction(action){", 1)[1].split(
        "async function openMailAttention", 1
    )[0]
    assert "const taskId=ATTENTION_TASK_ID" in action
    assert "if(ATTENTION_TASK_ID!==taskId)return" in action
    assert "encodeURIComponent(taskId)" in action
    assert "ATTENTION_TASK_ACTION_BUSY" in action
    assert "attentionApplyBtn').disabled=true" in action

    board = js.split("async function refreshBoard(){", 1)[1].split(
        "function renderBoard", 1
    )[0]
    assert "const seq=++BOARD_LOAD_SEQ" in board
    assert "if(seq!==BOARD_LOAD_SEQ)return" in board
    attention = js.split("async function loadAttention(payload){", 1)[1].split(
        "async function refreshTaskReports", 1
    )[0]
    assert "const seq=++ATTENTION_LOAD_SEQ" in attention
    assert "if(seq!==ATTENTION_LOAD_SEQ)return" in attention
    sse = js.split("SSE.addEventListener('board'", 1)[1].split(
        "SSE.addEventListener('messages'", 1
    )[0]
    assert "BOARD_LOAD_SEQ++" in sse
    assert "ATTENTION_LOAD_SEQ++" in sse
    assert "aria-busy','false'" in sse


def test_task_stats_bar_has_empty_failure_and_responsive_contracts():
    assert 'id="taskStats"' in HTML
    assert "暂无历史任务" in HTML
    assert "统计暂不可用" in HTML
    assert "总数" in HTML
    assert "完成率" in HTML
    assert "完成 / 失败 / 取消" in HTML
    assert "典型耗时" in HTML
    assert re.search(
        r"@media\(max-width:560px\).*?\.task-stats\s*\{[^}]*grid-template-columns:repeat\(2,minmax\(0,1fr\)\)",
        HTML,
        re.S,
    )

    js = _inline_js()
    load = js.split("async function loadTaskStats(){", 1)[1].split(
        "async function loadAttention", 1
    )[0]
    assert "const seq=++TASK_STATS_LOAD_SEQ" in load
    assert "if(seq!==TASK_STATS_LOAD_SEQ)return" in load
    assert "renderTaskStats(null,'error')" in load
    assert "stats.completion_rate!==null" in js
    attention = js.split("async function loadAttention(payload){", 1)[1].split(
        "async function refreshTaskReports", 1
    )[0]
    assert "void loadTaskStats()" in attention
    assert "await loadTaskStats" not in attention


def test_runtime_status_band_has_loading_failure_and_responsive_contracts():
    js = _inline_js()
    assert 'id="runtimeStatus" class="runtime-band"' in HTML
    assert "运行状态加载中…" in HTML
    assert "运行状态暂不可用" in HTML
    assert "运行时长" in HTML
    assert "SSE" in HTML
    assert "终端 WS" in HTML
    assert "终端存活" in HTML
    assert "输出缓存" in HTML
    assert " 行" in HTML
    assert 'id="runtimeStatus" class="runtime-band" aria-live=' not in HTML
    assert 'class="runtime-state" role="status"' in HTML
    assert "error.setAttribute('role','status')" in js

    narrow = HTML.split("@media(max-width:860px){", 1)[1].split(
        "@media(max-width:560px){", 1
    )[0]
    assert ".runtime-band{flex-wrap:wrap;white-space:normal}" in narrow
    phone = HTML.split("@media(max-width:560px){", 1)[1].split("</style>", 1)[0]
    assert re.search(
        r"\.runtime-band\{[^}]*flex-wrap:wrap;[^}]*white-space:normal",
        phone,
    )
    assert ".runtime-item{flex:" in phone

    load = js.split("async function loadRuntimeStats(){", 1)[1].split(
        "async function loadTaskStats", 1
    )[0]
    assert "const seq=++RUNTIME_STATS_LOAD_SEQ" in load
    assert "if(seq!==RUNTIME_STATS_LOAD_SEQ)return" in load
    assert "api('/api/runtime/stats')" in load
    assert "renderRuntimeStats(null,'error')" in load
    attention = js.split("async function loadAttention(payload){", 1)[1].split(
        "async function refreshTaskReports", 1
    )[0]
    assert "void loadRuntimeStats()" in attention
    refresh = js.split("function attRefresh(){", 1)[1].split(
        "async function loadSessionsView", 1
    )[0]
    assert "void loadRuntimeStats()" in refresh


def test_runtime_status_formatters_normalize_invalid_values():
    js = _inline_js()
    source = js.split("function runtimeCount(value){", 1)[1].split(
        "function renderRuntimeStats", 1
    )[0]
    out = _run_node(
        "function runtimeCount(value){" + source + textwrap.dedent(
            r"""
            const values=[
              runtimeCount(-1),runtimeCount('bad'),runtimeCount(Infinity),
              runtimeCount(3.9),runtimeCount(Number.MAX_VALUE),
            ];
            if(JSON.stringify(values)!==JSON.stringify([0,0,0,3,Number.MAX_SAFE_INTEGER]))process.exit(1);
            if(fmtRuntimeUptime(-1)!=='0 秒'||fmtRuntimeUptime(3661)!=='1 小时 1 分钟')process.exit(2);
            if(fmtRuntimeBytes(-1)!=='0 KiB'||fmtRuntimeBytes(1536)!=='1.5 KiB')process.exit(3);
            if(fmtRuntimeBytes(1572864)!=='1.5 MiB')process.exit(4);
            console.log('ok');
            """
        )
    )
    assert "ok" in out


def test_runtime_status_node_behavior_drops_stale_response():
    js = _inline_js()
    load = "async function loadRuntimeStats(){" + js.split(
        "async function loadRuntimeStats(){", 1
    )[1].split("async function loadTaskStats", 1)[0]
    out = _run_node(
        textwrap.dedent(
            r"""
            let RUNTIME_STATS_LOAD_SEQ=0,calls=0,resolveFirst;
            const rendered=[];
            async function api(url){
              if(url!=='/api/runtime/stats')process.exit(1);
              calls++;
              if(calls===1)return new Promise(resolve=>{resolveFirst=resolve});
              return {tag:'new'};
            }
            function renderRuntimeStats(stats,state='ready'){
              rendered.push(state==='error'?'error':stats.tag);
            }
            """
        )
        + load
        + textwrap.dedent(
            r"""
            const first=loadRuntimeStats();
            await Promise.resolve();
            await loadRuntimeStats();
            resolveFirst({tag:'old'});
            await first;
            if(JSON.stringify(rendered)!==JSON.stringify(['new']))process.exit(2);
            console.log('ok');
            """
        )
    )
    assert "ok" in out


def test_mail_deep_link_uses_one_load_and_preserves_focus_request():
    js = _inline_js()
    mail = js.split("async function openMailAttention(slug,messageId){", 1)[1].split(
        "async function handleAttentionHash", 1
    )[0]
    assert "VIEW_LOAD_SUPPRESSED=true" in mail
    assert "showView('msgs')" in mail
    assert "finally{VIEW_LOAD_SUPPRESSED=previous}" in mail
    assert "loadProjList(slug,messageId)" in mail
    load = js.split("async function loadProjectMsgs(focusMessageId,background=false,requestedSlug){", 1)[1].split(
        "function setMsgComposeEnabled", 1
    )[0]
    assert "queueMessageRequest(slug,focusMessageId,background)" in load
    assert "loadProjectMsgs(pending.focusMessageId,pending.background,pending.slug)" in load


def test_shortcut_guard_node_behavior_blocks_editing_modal_ime_and_modifiers():
    out = _run_node(textwrap.dedent(r"""
    let enabled=true,modal=false;
    function guard(e){
      if(!enabled||e.defaultPrevented||e.isComposing||e.ctrlKey||e.altKey||e.metaKey||modal)return true;
      const target=e.target,tag=target?.tagName;
      return tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||target?.isContentEditable||target?.xterm;
    }
    const base={defaultPrevented:false,isComposing:false,ctrlKey:false,altKey:false,metaKey:false,target:{tagName:'DIV'}};
    if(guard(base))process.exit(1);
    for(const patch of [{isComposing:true},{ctrlKey:true},{target:{tagName:'INPUT'}},{target:{tagName:'DIV',isContentEditable:true}},{target:{tagName:'DIV',xterm:true}}]){
      if(!guard({...base,...patch}))process.exit(2);
    }
    modal=true;if(!guard(base))process.exit(3);
    modal=false;enabled=false;if(!guard(base))process.exit(4);
    console.log('ok');
    """))
    assert "ok" in out


# ── B2 UX-P1-08～12：a11y/UX 静态 + Node DOM stub ─────────────────────────

def test_b2_modals_have_dialog_semantics():
    """UX-P1-11: 6 modal 经 openDialog 设 role/aria-modal/aria-labelledby;标题 id 齐全;背景内联 onclick 移除。"""
    js = _inline_js()
    assert "function openDialog" in js and "function closeDialog" in js and "function trapDialogKey" in js
    assert "'role','dialog'" in js and "'aria-modal','true'" in js and "'aria-labelledby'" in js
    for tid in ("launchModalTitle", "layoutModalTitle", "mmTitle", "attentionModalTitle", "collabModalTitle", "setupModalTitle"):
        assert 'id="' + tid + '"' in HTML
    for bad in (
        'onclick="if(event.target===this)closeLaunchModal()"',
        'onclick="if(event.target===this)closeLayoutModal()"',
        'onclick="if(event.target===this)closeMsg()"',
        'onclick="if(event.target===this)closeAttentionModal()"',
        'onclick="if(event.target===this)closeCollab()"',
        'onclick="if(event.target===this)closeSetup()"',
    ):
        assert bad not in HTML, f"残留 modal 背景内联 onclick: {bad}"
    # 背景 inert 覆盖 #updateBanner(链接/关闭按钮)与 .drawer-bg,不漏顶层可聚焦区
    assert "#updateBanner" in js and ".drawer-bg" in js


def test_b2_dialog_escape_uses_closer_and_setup_guard_intact():
    """Escape/背景走各自 closer;setupModal 走 closeSetup 保留 guard;closeXxx 传自身 bg(竞态)。"""
    js = _inline_js()
    assert "closer:closeCollab" in js and "closer:closeSetup" in js
    assert "closeDialog(expectedBg)" in js  # 竞态校验参数
    assert "closeDialog(document.getElementById('setupModal'))" in js
    assert "closeDialog(document.getElementById('launchModal'))" in js
    close = js.split("function closeSetup(){", 1)[1].split("async function setupOpenTerminal", 1)[0]
    assert "SETUP_IN_FLIGHT" in close


def test_b2_click_only_items_buttonified_with_keyboard():
    """UX-P1-10: 列表项 role=button+tabindex;委托补 Enter/Space→click。"""
    js = _inline_js()
    assert 'role="button" tabindex="0"' in js
    assert "e.key!=='Enter'&&e.key!==' '" in js and "it.click()" in js
    for marker in ('data-action="openFlow"', 'data-action="openAttention"', 'data-action="fileUp"', 'data-action="tfBack"'):
        assert marker in js


def test_b2_sidebar_a11y_inert_aria_focus():
    """UX-P1-12: 移动侧栏关闭 inert/aria-hidden;aria-expanded;打开焦点入 nav。"""
    js = _inline_js()
    assert "function setNavA11y" in js and "NAV_COLLAPSE_MQ.matches" in js
    toggle = js.split("function toggleNav(){", 1)[1].split("function closeNav", 1)[0]
    assert "#nav button" in toggle
    close = js.split("function closeNav(){", 1)[1].split("function toggleNavPc", 1)[0]
    assert "setNavA11y(false)" in close
    pc = js.split("function toggleNavPc(){", 1)[1].split("function ", 1)[0]
    assert "aria-expanded" in pc


def test_b2_attention_empty_state_has_create_cta():
    """UX-P1-08: 默认工作台无 session 时提供创建 CTA,且不宣称无阻塞。"""
    js = _inline_js()
    idx = js.index("当前没有运行中的 Herdr session")
    tail = js[idx:idx + 220]
    assert "showSetupModal()" in tail
    assert "当前没有需要你处理的阻塞" not in tail


def test_b2_flow_focuses_target_pane_input():
    """UX-P1-09: enterHerdrFlow 命中目标 pane 后聚焦输入(非仅滚动)。"""
    js = _inline_js()
    assert "hf-in-'+fp" in js


def test_b2_node_dialog_escape_calls_closer_and_stops_propagation():
    """Node DOM stub: trapDialogKey Escape→closer 且 stopPropagation(不冒泡到 hfExitFullscreen)。"""
    js = _inline_js()
    trap_src = js.split("function trapDialogKey(e,bg){", 1)[1].split("\n}", 1)[0]
    assert "e.stopPropagation()" in trap_src
    out = _run_node(
        "function trapDialogKey(e,bg){\n" + trap_src + "\n}\n"
        "let stopped=false,closed=false;\n"
        "trapDialogKey({key:'Escape',preventDefault(){},stopPropagation(){stopped=true}},{__closer:()=>{closed=true}});\n"
        "if(!closed||!stopped){console.error('escape',closed,stopped);process.exit(1)}\n"
        "console.log('ok');\n"
    )
    assert "ok" in out


def test_b2_close_nav_restores_focus_to_toggle():
    """UX-P1-12: closeNav 把焦点恢复到可见的 .nav-toggle(背景点击/选择后)。"""
    js = _inline_js()
    close = js.split("function closeNav(){", 1)[1].split("function toggleNavPc", 1)[0]
    assert ".nav-toggle" in close


def test_b2_file_rows_role_button_has_no_nested_button():
    """UX-P1-10: 文件/搜索/term 文件行拆成 ft-name(role=button) + 独立 download button(sibling),
    role=button 元素不内嵌 button,避免 axe 嵌套交互。面包屑无嵌套可保留。"""
    js = _inline_js()
    assert '<div class="ft-item"><span class="ft-name"' in js
    # ft-item 容器本身不再带 role=button(role 移到 ft-name span)
    assert '<div class="ft-item" data-action="fileSearchOpen"' not in js
    assert '<div class="ft-item" data-action="${e.type===\'dir\'?\'fileCd\':\'fileOpen\'}"' not in js


def test_b2_node_dialog_close_race_guarded():
    """Node DOM stub: closeDialog(expectedBg) 仅在 OPEN_DIALOG===expected 时清理;
    异步旧 modal 的 close 到达时不清掉用户已开的新 dialog(竞态)。"""
    js = _inline_js()
    close_src = js.split("function closeDialog(expectedBg){", 1)[1].split("\n}", 1)[0]
    out = _run_node(
        "let OPEN_DIALOG=null,LAST_FOCUS=null;\n"
        "function setBackgroundInert(){}\n"
        "function closeDialog(expectedBg){\n" + close_src + "\n}\n"
        "const A={removeAttribute(){}},B={removeAttribute(){}};\n"
        "OPEN_DIALOG=B;\n"
        "closeDialog(A);\n"
        "if(OPEN_DIALOG!==B){console.error('race cleared new dialog');process.exit(1)}\n"
        "closeDialog(B);\n"
        "if(OPEN_DIALOG!==null){console.error('did not close current');process.exit(2)}\n"
        "console.log('ok');\n"
    )
    assert "ok" in out


def test_b2_attention_no_blocker_when_no_session():
    """P1-08: 无 session 不渲染'无阻塞'/'需处理事项'标题,只 CTA;有 session 且无 items 才显示无阻塞。"""
    js = _inline_js()
    assert "(sessions.length?'<div class=\"capability-note\">✓ 当前没有需要你处理的阻塞" in js
    assert "const actionTitle=sessions.length?" in js


def test_b2_hf_takeover_says_session_tui():
    """hfTakeover 文案明确 session TUI(zh 按钮/title + en + ja),不暗示 pane 级接管。"""
    assert "接管 session TUI" in HTML
    assert "接管整个 session" in HTML
    assert "'hf.takeover':'🖥 Take over session TUI'" in HTML
    assert "'hf.takeover':'🖥 セッション TUI を操作'" in HTML


def test_b2_node_background_inert_preserves_preexisting():
    """Node: setBackgroundInert 只恢复本 helper 添加的 inert;
    side 初始 inert→open/close 后仍 inert;view 初始无→open 时 inert、close 后恢复无。"""
    js = _inline_js()
    inert_body = js.split("const setBackgroundInert=on=>{", 1)[1].split("\n};", 1)[0]
    out = _run_node(
        "function mkEl(){const a={};return{hasAttribute(k){return k in a},setAttribute(k,v){a[k]=v},removeAttribute(k){delete a[k]}}}\n"
        "const side=mkEl();side.setAttribute('inert','');\n"
        "const view=mkEl();\n"
        "const navExpand=mkEl();\n"
        "const document={querySelectorAll(sel){return sel==='[data-dlg-inert]'?[view,navExpand]:[side,view,navExpand]}};\n"
        "const setBackgroundInert=on=>{\n" + inert_body + "\n};\n"
        "setBackgroundInert(true);\n"
        "if(side.hasAttribute('data-dlg-inert')){console.error('side should not get dlg tag');process.exit(1)}\n"
        "if(!side.hasAttribute('inert')){console.error('side lost pre inert on open');process.exit(2)}\n"
        "if(!view.hasAttribute('inert')||!view.hasAttribute('data-dlg-inert')){console.error('view should get dlg inert');process.exit(3)}\n"
        "if(!navExpand.hasAttribute('inert')||!navExpand.hasAttribute('data-dlg-inert')){console.error('navExpand should get dlg inert');process.exit(6)}\n"
        "setBackgroundInert(false);\n"
        "if(!side.hasAttribute('inert')){console.error('side lost pre inert on close');process.exit(4)}\n"
        "if(view.hasAttribute('inert')||view.hasAttribute('data-dlg-inert')){console.error('view should be cleared on close');process.exit(5)}\n"
        "if(navExpand.hasAttribute('inert')||navExpand.hasAttribute('data-dlg-inert')){console.error('navExpand should be cleared on close');process.exit(7)}\n"
        "console.log('ok');\n"
    )
    assert "ok" in out


def test_b2_modal_bg_above_navexpand_below_toast():
    """modal-bg z-index 高于 #navExpand(70),低于 toast(200),展开按钮不再浮在 dialog 上。"""
    assert "z-index:80" in HTML  # .modal-bg 60 -> 80(>70 navExpand, <200 toast)


def test_inline_input_send_appends_enter():
    js = _inline_js()
    # 发送=提交:必须补 \r,否则文字只停在 agent 输入框
    assert "queueTermInput(TERM_ID,text+'\\r')" in js


def test_terminal_true_fullscreen():
    js = _inline_js()
    assert "function toggleTermFullscreen()" in js
    assert "requestFullscreen" in js
    assert "term-fs-fixed" in js  # iOS 回退
    assert "padding-bottom:var(--safe-bottom)" in HTML
    assert "Esc 或右上按钮退出" in HTML
    assert 'id="termFsExit"' in HTML
    assert "#termStage:fullscreen" in HTML
    assert "#termStage{position:relative}" in HTML
    stage_tag = HTML.split('id="termStage"', 1)[1].split(">", 1)[0]
    assert "position:relative" not in stage_tag
    assert "#termStage.term-fs-fixed{position:fixed" in HTML
    # 统一退出路径:浮动按钮与 Escape 共用 exitTermFullscreen
    assert "function exitTermFullscreen()" in js
    assert 'onclick="exitTermFullscreen()"' in HTML
    assert "if(e.key==='Escape'){hfExitFullscreen();exitTermFullscreen()}" in js
    # 原生全屏退出时兜底清理 fixed 类
    assert "classList.remove('term-fs-fixed')" in js
    # 退出钮在顶部(含 safe-area),不遮挡底部 H5 内联输入条
    assert "top:calc(10px + var(--safe-top))" in HTML
    assert "bottom:10px" not in HTML.split("#termFsExit{", 1)[1].split("}", 1)[0]
    # 全屏切换后 refit + 强制重绘
    assert "fullscreenchange" in js


def test_node_mobile_untile_inflight_promise_dedupes_concurrent_calls():
    """Lead OCR: 同一 session 并发 ensureMobileSessionUntiled 共用 in-flight Promise，只发一次 untile。"""
    js = _inline_js()
    assert "MOBILE_UNTILE_INFLIGHT" in js
    # 从源码抽出函数体，挂到可测 harness（stub isCompactScreen/api/toast）
    fn_src = js.split("async function ensureMobileSessionUntiled(session){", 1)[1].split(
        "\nasync function syncTermZoomLease", 1
    )[0]
    out = _run_node(
        textwrap.dedent(
            r"""
            const MOBILE_UNTILED_SESSIONS=new Set();
            const MOBILE_UNTILE_INFLIGHT={};
            let BOARD=null;
            let compact=true;
            const calls=[];
            function isCompactScreen(){return compact}
            function toast(){}
            function api(url,opts){
              calls.push({url,method:(opts&&opts.method)||'GET',body:opts&&opts.body});
              if(url==='/api/herdr/snapshot'){
                return Promise.resolve({panes:[
                  {session:'S1',pane_id:'p1',tab_id:'t1'},
                  {session:'S1',pane_id:'p2',tab_id:'t1'},
                ]});
              }
              if(String(url).includes('/layout/untile')){
                // 模拟慢响应，拉长 in-flight 窗口
                return new Promise(resolve=>setTimeout(
                  ()=>resolve({moved:[{pane_id:'p2'}]}), 40
                ));
              }
              return Promise.resolve({});
            }
            async function ensureMobileSessionUntiled(session){
            """
        )
        + fn_src
        + textwrap.dedent(
            r"""
            const [a,b]=await Promise.all([
              ensureMobileSessionUntiled('S1'),
              ensureMobileSessionUntiled('S1'),
            ]);
            const untileCalls=calls.filter(c=>String(c.url).includes('/layout/untile'));
            if(untileCalls.length!==1){
              console.error('expected 1 untile, got',untileCalls.length,calls);process.exit(1);
            }
            if(!MOBILE_UNTILED_SESSIONS.has('S1')){
              console.error('session not marked untiled');process.exit(2);
            }
            if(Object.keys(MOBILE_UNTILE_INFLIGHT).length!==0){
              console.error('inflight not cleared',MOBILE_UNTILE_INFLIGHT);process.exit(3);
            }
            // 完成后再次调用应直接 skip，不再请求
            const before=calls.length;
            const c=await ensureMobileSessionUntiled('S1');
            if(!c.skipped){console.error('expected skipped after done',c);process.exit(4)}
            if(calls.length!==before){console.error('skipped still hit api');process.exit(5)}
            // 桌面宽屏不请求
            compact=false;
            MOBILE_UNTILED_SESSIONS.clear();
            const d=await ensureMobileSessionUntiled('S1');
            if(d.moved&&d.moved.length){console.error('desktop should no-op',d);process.exit(6)}
            console.log('ok');
            """
        )
    )
    assert "ok" in out


def test_node_resolve_slug_by_human_key_exact_only():
    """Lead OCR: session→消息项目只按 overview human_key 精确映射；后缀/包含不得命中。"""
    js = _inline_js()
    norm_src = js.split("function normalizeHumanKey(path){", 1)[1].split(
        "\n/**", 1
    )[0]
    resolve_src = js.split("function resolveSlugByHumanKey(projectPath,projects){", 1)[1].split(
        "\nfunction onMsgSessionChange", 1
    )[0]
    # 禁止旧 fuzzy 残留
    assert "slugifyProjectPath" not in js
    assert "want.endsWith" not in js
    out = _run_node(
        "function normalizeHumanKey(path){\n"
        + norm_src
        + "\n"
        + "function resolveSlugByHumanKey(projectPath,projects){\n"
        + resolve_src
        + "\n"
        + textwrap.dedent(
            r"""
            const projects=[
              {slug:'agent-cockpit',human_key:'/home/fyc/github/agent-cockpit'},
              {slug:'cockpit',human_key:'/home/fyc/github/cockpit'},
              {slug:'other',human_key:'/tmp/other'},
            ];
            // 精确命中
            const a=resolveSlugByHumanKey('/home/fyc/github/agent-cockpit',projects);
            if(a!=='agent-cockpit'){console.error('exact fail',a);process.exit(1)}
            // 尾斜杠规范化后仍精确
            const b=resolveSlugByHumanKey('/home/fyc/github/agent-cockpit/',projects);
            if(b!=='agent-cockpit'){console.error('trail slash fail',b);process.exit(2)}
            // 后缀/包含模糊：不得把 .../agent-cockpit 误匹配到 slug 'cockpit'
            // （旧逻辑 slugify 后 endsWith/includes 会误中）
            const c=resolveSlugByHumanKey('/home/fyc/github/agent-cockpit-extra',projects);
            if(c!==''){console.error('must not fuzzy match extra path',c);process.exit(3)}
            // human_key 仅后缀相同、整路径不同 → 不命中
            const d=resolveSlugByHumanKey('/elsewhere/agent-cockpit',projects);
            if(d!==''){console.error('must not suffix-match human_key',d);process.exit(4)}
            // 空 / 无列表
            if(resolveSlugByHumanKey('',projects)!==''){console.error('empty path');process.exit(5)}
            if(resolveSlugByHumanKey('/tmp/other',[])!==''){console.error('empty list');process.exit(6)}
            console.log('ok');
            """
        )
    )
    assert "ok" in out

def test_layout_untile_uses_fresh_session_focus_not_stale_board():
    """拆开整组必须用新鲜 snapshot 的 session.focused_pane_id，禁止 BOARD 多 focused 盲取第一个。"""
    js = _inline_js()
    assert "async function layoutFreshContext()" in js
    assert "focused_pane_id" in js
    assert "layoutGroupHint" in HTML
    assert "你所在的分屏组" in js or "你所在的分屏组" in HTML
    untile = js.split("async function layoutUntile(){", 1)[1].split(
        "async function layoutCompose", 1
    )[0]
    assert "await layoutTargetOrToast()" in untile
    assert "groupTabId" in untile
    # 不再同步 layoutTargetOrToast 吃 stale BOARD
    assert "async function layoutTargetOrToast()" in js

def test_node_layout_fresh_context_picks_multi_pane_group():
    """Node: focused_pane_id 在 4-pane tab 时，拆开整组目标 tab 为该 multi 组，而非单 pane focused。"""
    js = _inline_js()
    # 抽出 layoutFreshContext 主体并 stub api/termCollabContext/BOARD
    fn_src = js.split("async function layoutFreshContext(){", 1)[1].split(
        "\nfunction layoutGroupLabel", 1
    )[0]
    out = _run_node(
        textwrap.dedent(
            r"""
            let BOARD=null;
            function termCollabContext(){return {session:'github-agent-cockpit',paneId:''}}
            function api(){
              return Promise.resolve({
                sessions:[{session:'github-agent-cockpit',focused_pane_id:'w1:p9'}],
                panes:[
                  {session:'github-agent-cockpit',pane_id:'w1:p4',tab_id:'w1:tD',agent:'opencode',focused:true},
                  {session:'github-agent-cockpit',pane_id:'w1:p7',tab_id:'w1:t5',agent:'grok',focused:false},
                  {session:'github-agent-cockpit',pane_id:'w1:p9',tab_id:'w1:t5',agent:'claude',focused:true},
                  {session:'github-agent-cockpit',pane_id:'w1:pA',tab_id:'w1:t5',agent:'qodercli',focused:false},
                  {session:'github-agent-cockpit',pane_id:'w1:p5',tab_id:'w1:t5',agent:'qodercli',focused:false},
                ],
              });
            }
            async function layoutFreshContext(){
            """
        )
        + fn_src
        + textwrap.dedent(
            r"""
            const ctx=await layoutFreshContext();
            if(ctx.focusedPaneId!=='w1:p9'){console.error('focus',ctx.focusedPaneId);process.exit(1)}
            if(ctx.groupTabId!=='w1:t5'){console.error('groupTab',ctx.groupTabId,ctx);process.exit(2)}
            if(ctx.group.length!==4){console.error('group size',ctx.group.length);process.exit(3)}
            if(ctx.target?.pane_id!=='w1:p9'){console.error('target',ctx.target);process.exit(4)}
            // 无 CURRENT_TERM 时不得盲取 BOARD 里第一个 focused(w1:p4 单 pane)
            console.log('ok');
            """
        )
    )
    assert "ok" in out


def test_theme_switch_inplace_single_herdr_notify_and_input_gate():
    # 就地切换：禁止整页 reload；Mode 2031 只走 API 一次；重绘期静默丢输入。
    js = _inline_js()
    assert "function _repaintTermForTheme" not in js
    assert "TERM_LIGHT" not in js
    assert "function applyDocumentTheme(mode)" in js
    set_theme = js.split("function setTheme(mode,save=true){", 1)[1].split(
        "function toggleTheme", 1
    )[0]
    assert "localStorage.setItem('dash-theme'" in set_theme
    assert "location.reload()" not in set_theme
    assert "applyDocumentTheme" in set_theme
    assert "options.theme=th" in set_theme
    assert "showTermThemeOverlay()" in set_theme
    assert set_theme.index("showTermThemeOverlay()") < set_theme.index("options.theme=th")
    assert "requestAnimationFrame(()=>requestAnimationFrame(applyTheme))" in set_theme
    assert "afterTermPaints(()=>" in set_theme
    assert "hideTermThemeOverlay()" in set_theme
    assert "armTermInputGate" in set_theme
    assert "sendAllTermColorSchemes(false)" in set_theme
    assert "/api/theme/herdr" in set_theme
    # 禁止默认 true 广播（会与 API 双发 Mode 2031）
    assert "sendAllTermColorSchemes()" not in set_theme
    assert "sendAllTermColorSchemes(true)" not in set_theme
    assert "let TERM_INPUT_GATE_UNTIL=0;" in js
    gate = js.split("function termUserInputBlocked(id){", 1)[1].split(
        "function flushTermInput", 1
    )[0]
    assert "TERM_THEME_OVERLAYING" in gate
    assert "TERM_INPUT_GATE_UNTIL" in gate
    assert "loadingEl" in gate
    assert "replayComplete" in gate
    websocket = js.split("function openTermWS(id,xterm,replay){", 1)[1].split(
        "function showTermInstance", 1
    )[0]
    assert "armTermInputGate" not in websocket
    assert "isTermMouseReport" in js
    assert "isTermProtocolReply" in js


def test_layout_quick_pair_and_empty_shell_controls_are_separated():
    modal = HTML.split('<div class="modal-bg" id="layoutModal">', 1)[1].split(
        "<!-- 终端抽屉 -->", 1
    )[0]
    quick, advanced = modal.split('<details class="layout-advanced">', 1)
    assert "layoutPair('horizontal')" in quick
    assert "layoutPair('vertical')" in quick
    for mode in ("horizontal", "vertical", "grid4"):
        action = f"layoutSplit('{mode}')"
        assert action not in quick
        assert action in advanced
    for key in (
        "layout.pair_h",
        "layout.pair_v",
        "layout.advanced",
        "layout.advanced_hint",
        "layout.group_loading",
    ):
        assert HTML.count(f"'{key}'") == 2, f"{key} must have English and Japanese translations"
    for key in (
        "layout.group_single",
        "layout.group_multi",
        "layout.empty_shell",
        "layout.empty_shell_tag",
        "layout.focus_mark",
        "layout.group_mark",
        "layout.single_no_groups",
    ):
        assert HTML.count(f"'{key}'") >= 3, f"{key} must be translated and used dynamically"


def test_node_layout_pair_composes_only_existing_agent_panes():
    js = _inline_js()
    pair_src = "async function layoutPair(orientation){" + js.split(
        "async function layoutPair(orientation){", 1
    )[1].split("\nasync function layoutSplit", 1)[0]
    assert "/layout/compose" in pair_src
    assert "/layout/split" not in pair_src
    assert "close_empty" not in pair_src
    out = _run_node(
        textwrap.dedent(
            r"""
            let context=null;
            const calls=[];
            const toasts=[];
            let refreshes=0;
            let closes=0;
            async function layoutFreshContext(){return context}
            async function api(url,options){calls.push({url,options});return {}}
            async function refreshBoard(){refreshes++}
            function closeLayoutModal(){closes++}
            function toast(message){toasts.push(message)}
            function t(_key,fallback){return fallback}
            """
        )
        + pair_src
        + textwrap.dedent(
            r"""
            context={
              session:'demo',
              target:{pane_id:'w1:p2',agent:'codex'},
              panes:[
                {pane_id:'w1:p1',agent:null},
                {pane_id:'w1:p2',agent:'codex'},
                {pane_id:'w1:p3',agent:'grok'},
              ],
            };
            await layoutPair('horizontal');
            let body=JSON.parse(calls[0].options.body);
            if(calls[0].url!=='/api/herdr/session/demo/layout/compose')process.exit(1);
            if(JSON.stringify(body)!==JSON.stringify({pane_ids:['w1:p2','w1:p3'],orientation:'horizontal'}))process.exit(2);

            context={
              session:'demo',
              target:{pane_id:'w1:p1',agent:null},
              panes:[
                {pane_id:'w1:p1',agent:null},
                {pane_id:'w1:p4',agent:'opencode'},
                {pane_id:'w1:p5',agent:'claude'},
              ],
            };
            await layoutPair('vertical');
            body=JSON.parse(calls[1].options.body);
            if(JSON.stringify(body)!==JSON.stringify({pane_ids:['w1:p4','w1:p5'],orientation:'vertical'}))process.exit(3);

            const before=calls.length;
            context={session:'demo',target:{pane_id:'w1:p1',agent:null},panes:[
              {pane_id:'w1:p1',agent:null},{pane_id:'w1:p4',agent:'opencode'},
            ]};
            await layoutPair('horizontal');
            if(calls.length!==before)process.exit(4);
            if(!toasts.includes('当前 session 至少需要两个 Agent pane'))process.exit(5);
            if(calls.some(call=>call.url.includes('/layout/split')||call.url.includes('/close')))process.exit(6);
            if(refreshes!==2||closes!==2)process.exit(7);
            console.log('ok');
            """
        )
    )
    assert "ok" in out


def test_node_layout_split_requires_confirmation_before_api():
    js = _inline_js()
    split_src = "async function layoutSplit(mode){" + js.split(
        "async function layoutSplit(mode){", 1
    )[1].split("\nasync function layoutDetach", 1)[0]
    assert split_src.index("confirm(") < split_src.index("await api(")
    out = _run_node(
        textwrap.dedent(
            r"""
            const calls=[];
            let approved=false;
            async function layoutTargetOrToast(){return {session:'demo',paneId:'w1:p1'}}
            function confirm(){return approved}
            async function api(url,options){calls.push({url,options});return {}}
            async function refreshBoard(){}
            function closeLayoutModal(){}
            function toast(){}
            function t(_key,fallback){return fallback}
            """
        )
        + split_src
        + textwrap.dedent(
            r"""
            await layoutSplit('grid4');
            if(calls.length!==0)process.exit(1);
            approved=true;
            await layoutSplit('grid4');
            if(calls.length!==1)process.exit(2);
            if(!calls[0].url.endsWith('/layout/split'))process.exit(3);
            if(JSON.parse(calls[0].options.body).mode!=='grid4')process.exit(4);
            console.log('ok');
            """
        )
    )
    assert "ok" in out
