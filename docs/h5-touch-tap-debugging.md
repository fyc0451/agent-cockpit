# H5 真机触摸点击失效排查指南（TUI 鼠标事件）

> 2026-08-09 定位并修复（commit `fcdcd7e`）。ZCode 曾长时间排查未果，本文档供后续 agent 快速上手。

## 症状

手机真机 H5 页面里，herdr TUI（switch 面板、pane 列表等）**点击完全无反应**；PC Chrome 设备模拟器里点击正常。滑动正常。

## 根因（已实证，勿再猜）

真机 tap 的事件序列（事件记录仪实测）：

```
pointerdown → touchstart → pointerup → touchend
（之后什么都没有：没有 mousedown/mouseup/click）
```

- tap 落在 xterm 的 `.xterm-link-layer` 上时，**xterm 5.x 的手势识别层**（LinkHoverTracker 系）dispatch TAP 手势后会对 touchstart 调 `preventDefault()`；
- 浏览器因此**抑制兼容性鼠标事件**（compat mouse events：touchend 后补发的 mousedown/mouseup/click）；
- xterm 的鼠标协议转发（MouseService）只监听 mousedown 系列 → TUI 收不到任何字节；
- **PC 设备模拟器走的是另一套事件管线**（直接产鼠标事件），所以模拟器测不出来——这是本问题最大的坑。

## 修复（已在主线）

`static/index.html` 的 `enableTermTouchScroll()` 末尾：**tap→click 合成**。

- `touchstart` 记录起点（仅单指、且 `xterm.modes.mouseTrackingMode !== 'none'` 即 TUI 开了鼠标追踪时）；
- `touchmove` 移动 >10px 标记为滚动，不合成；
- `touchend`：未移动且 ≤500ms → 延迟 30ms 合成 `mousedown`/`mouseup`/`click`（`elementFromPoint` 定位目标、冒泡、带坐标）；
- 若 400ms 内浏览器自己派发了可信 `mousedown`（`e.isTrusted`）→ 取消合成，避免双触发。

合成事件是 untrusted，**不会触发 focus/弹软键盘**，与"点终端不弹键盘"的拦截（capture 阶段拦 `xterm-helper-textarea` 的 focusin）不冲突。

## 已排除的假设（不要再试）

1. ~~focusin 拦截的 blur/stopImmediatePropagation 打断鼠标转发~~
2. ~~`touch-action:none` 阻止 tap→click 合成~~（改 manipulation 会把 PC 搞坏）
3. ~~触摸滚动桥 move() 的 preventDefault 吞 tap~~（加阈值无效）
4. ~~"PC 正常"~~：只有模拟器正常，PC 真实鼠标在受影响版本上同样可能失败——**任何"模拟器正常"的结论都不能外推到真机**。

## 排查方法（可复用）

真机事件记录仪：capture 阶段监听 `touchstart/touchend/pointerdown/pointerup/mousedown/mouseup/click`，目标限终端容器，叠加显示在页面底部；同时钩住发往 PTY 的数据（鼠标序列以 `\x1b[<` 开头）。关键判断点：

- 真机 tap 后**有无 mousedown/click** → 没有 = 兼容鼠标事件被抑制（本问题）；
- 有无 `PTY<<` 鼠标序列出站 → 没有 = xterm 没转发。

> 完整记录仪代码见 session 记录 `2026-08-09`（kimi-code H5 排查）。用 `?debug=1` 门控，用完即撤，不进主线。

## 相关代码位置

- 触摸桥：`static/index.html` → `enableTermTouchScroll(el, xterm)`
- xterm 手势层：vendored `xterm.js` 内 `_handleTouchStart`（`this._dispatched && e.preventDefault()`）
- 键盘拦截：`focusin` capture 拦截 `xterm-helper-textarea`

## 附录：记录仪完整代码（粘贴到 index.html </body> 前，?debug=1 门控）

```html
<script>
(function(){
if(!/[?&]debug=1/.test(location.search))return;
const box=document.createElement('div');
box.style.cssText='position:fixed;left:0;right:0;bottom:0;max-height:38vh;overflow:auto;background:rgba(0,0,0,.88);color:#7fff9a;font:10px/1.35 monospace;z-index:99999;padding:4px 6px;white-space:pre-wrap;pointer-events:none';
document.body.appendChild(box);
const lines=[];
function log(msg){const t=(performance.now()/1000).toFixed(2);lines.push(t+' '+msg);if(lines.length>120)lines.shift();box.textContent=lines.join('\n');box.scrollTop=box.scrollHeight;}
window.__h5dbg=log;
const EVS=['touchstart','touchend','touchcancel','pointerdown','pointerup','pointercancel','mousedown','mouseup','click','wheel'];
for(const type of EVS){
  document.addEventListener(type,e=>{
    if(!(e.target.closest&&e.target.closest('#termContainer,.drawer,#view-herdrflow')))return;
    const pt=e.changedTouches?e.changedTouches[0]:e;
    log(type+' @'+(e.target.className||e.target.tagName||'').toString().slice(0,28)+' x='+Math.round(pt.clientX||0)+' y='+Math.round(pt.clientY||0)+(e.defaultPrevented?' PD':''));
  },{capture:true,passive:true});
}
document.addEventListener('focusin',e=>{if(e.target.tagName==='TEXTAREA')log('focusin textarea')},{capture:true});
window.visualViewport&&visualViewport.addEventListener('resize',()=>log('viewport h='+Math.round(visualViewport.height)));
})();
</script>
```
判断：真机 tap 后无 mousedown/click 行 = 兼容鼠标事件被抑制（本问题）。
