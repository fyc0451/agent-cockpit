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
    # 不主动读取/劫持 Mac 剪贴板；原生 Command+V 继续由浏览器/xterm 处理。
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
    assert HTML.count("📋 复制到 Mac") >= 2
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


def test_cdn_assets_use_subresource_integrity():
    # 终端依赖来自 CDN；固定版本仍需 SRI，避免 CDN 内容被替换后取得同源 API 权限
    for package in ("@xterm/xterm@5.5.0", "@xterm/addon-fit@0.10.0"):
        tags = [line for line in HTML.splitlines() if package in line]
        assert tags
        assert all('integrity="sha384-' in line for line in tags)
        assert all('crossorigin="anonymous"' in line for line in tags)


def test_message_fields_escaped():
    # m.importance 可由 API 写入,不得裸进 innerHTML;fmtTime/m.id/a.unread 同理 esc
    assert "${m.importance" not in HTML
    assert "${esc(m.importance" in HTML
    assert "${esc(fmtTime(m.created_ts))}" in HTML
    assert 'card-badge">${esc(a.unread)}' in HTML
    # data-pid/data-mid 属性上下文也 esc(防御契约变化)
    assert 'data-pid="${esc(d.project.id)}"' in HTML
    assert 'data-mid="${esc(m.id)}"' in HTML


def test_setup_workspace_output_escaped():
    # r.started/r.notified 来自后端响应,经 map(esc) 后再 join
    assert "r.started.map(esc).join" in HTML
    assert "(r.notified||[]).map(esc).join" in HTML


def test_attach_herdr_validates_session_name():
    # session 拼进发给 PTY 的 shell 命令,先用与后端一致的白名单校验,非法则不写 WS
    assert "/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/" in HTML
    assert "validSessionName(session)" in HTML
    assert "if(!validSessionName(session))" in HTML


def test_attach_herdr_waits_for_its_websocket():
    js = _inline_js()
    assert "function queueTermInput(id,data,notice)" in js
    assert "flushTermInput(id,ws)" in js
    assert "queueTermInput(r.id,'herdr --session '+session+'\\r'" in js
    assert "// 等 WS 连上,发 herdr attach 命令\n    setTimeout" not in js


def test_h5_layout_uses_visible_dynamic_viewport():
    # 手机浏览器地址栏/键盘会改变可视高度；页面与全屏抽屉必须跟随 dvh。
    assert "height:100dvh" in HTML
    assert ".drawer-bg.show{inset:var(--safe-top) var(--safe-right) var(--safe-bottom) var(--safe-left);padding:0}" in HTML
    assert ".drawer{height:100%;max-height:100%" in HTML
    # 顶部导航与终端工具栏在窄屏必须换行，不能被 body overflow:hidden 裁掉。
    assert "header nav{order:3;width:100%" in HTML
    assert ".term-toolbar{flex-wrap:wrap" in HTML
    assert "visualViewport" in HTML


def test_h5_safe_area_and_overlay_scroll_are_contained():
    assert "viewport-fit=cover" in HTML
    assert "--safe-top:env(safe-area-inset-top,0px)" in HTML
    assert "padding:var(--safe-top) var(--safe-right) var(--safe-bottom) var(--safe-left)" in HTML
    assert "bottom:calc(64px + var(--safe-bottom))" in HTML
    assert "overscroll-behavior:none" in HTML
    assert "overscroll-behavior:contain" in HTML


def test_mobile_connections_and_focused_input_recover():
    js = _inline_js()
    assert "document.addEventListener('visibilitychange'" in js
    assert "document.visibilityState==='visible'" in js
    assert "connectSSE();refreshBoard()" in js
    assert "function scheduleTermReconnect(id)" in js
    assert "reconnectDelay" in js
    assert "Math.min(delay*2,10000)" in js
    assert "function keepFocusedControlVisible()" in js
    assert "scrollIntoView({block:'nearest'})" in js


def test_h5_can_switch_herdr_panes_without_keyboard_shortcuts():
    js = _inline_js()
    assert 'id="paneSelect"' in HTML
    assert "function syncPaneSelect()" in js
    assert "function switchDrawerPane(paneId)" in js
    assert "function stepDrawerPane(delta)" in js
    # H5 点击 herdr 直接进入 pane 抽屉，顶部 select/前后按钮切换，不依赖 Ctrl-b。
    assert "isCompactScreen()" in js
    assert "openTerm(session,panes[0].pane_id" in js
    # 切换时旧 pane 的慢响应不能覆盖新 pane 内容。
    assert "CURRENT_TERM.paneId!==t.paneId" in js


def test_herdr_flow_is_mobile_friendly_default_card_view():
    js = _inline_js()
    assert 'data-view="herdrflow"' in HTML
    assert 'id="view-herdrflow"' in HTML
    assert 'data-action="openFlow"' in HTML
    assert "enterHerdrFlow(it.dataset.session,it.dataset.pane)" in js
    assert ".hf-out{" in HTML
    assert "user-select:text" in HTML
    assert "HF_TIMER=setInterval(hfRefreshAll,3000)" in js
    assert "function hfStop()" in js


def test_herdr_flow_preserves_selection_and_ignores_stale_refresh():
    js = _inline_js()
    assert "HF_SESSION!==session||!out.isConnected" in js
    assert "out.textContent===next" in js
    assert "out.contains(selection.anchorNode)" in js
    assert "function updatePaneOutput(out,next)" in js
    assert "out.scrollHeight-out.scrollTop-out.clientHeight<=2" in js
    assert "const top=out.scrollTop" in js
    assert "out.scrollTop=follow?out.scrollHeight:top" in js
    assert js.count("updatePaneOutput(out,next)") >= 2
    assert "out.innerHTML=d.output" not in js
    assert "encodeURIComponent(p.session)" in js
    assert "encodeURIComponent(p.pane_id)" in js


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
