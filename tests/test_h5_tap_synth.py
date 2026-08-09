"""tests/test_h5_tap_synth.py — tap→click 合成的 Node 行为测试。

从 static/index.html 抽取 enableTermTouchScroll 源码,用桩 DOM 在 Node 里
驱动真实函数,覆盖 codex #1522 要求的行为用例。
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const m = html.match(/<script>([\s\S]*)<\/script>/);
const src = m[1];
// 按花括号配对抽取 enableTermTouchScroll 函数体
const start = src.indexOf('function enableTermTouchScroll');
let i = src.indexOf('{', start), depth = 0, end = -1;
for (let j = i; j < src.length; j++) {
  if (src[j] === '{') depth++;
  else if (src[j] === '}') { depth--; if (depth === 0) { end = j + 1; break; } }
}
const fnSrc = src.slice(start, end);

// ── 桩 ──
class FakeMouseEvent {
  constructor(type, init) { this.type = type; Object.assign(this, init); }
}
globalThis.MouseEvent = FakeMouseEvent;
globalThis.WheelEvent = class { constructor(type, init) { this.type = type; Object.assign(this, init); this.defaultPrevented = false; } };
globalThis.window = {};
const docListeners = {};
globalThis.document = { addEventListener(t, f) { (docListeners[t] = docListeners[t] || []).push(f); } };

function makeEl() {
  const listeners = {};
  return {
    listeners,
    isConnected: true,
    contains(t) { return this._targets ? this._targets.has(t) : true; },
    querySelector() { return null; },
    addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
    fire(t, e) { (listeners[t] || []).forEach(f => f(e)); },
  };
}
function makeXterm() {
  return {
    modes: { mouseTrackingMode: 'x10' },
    buffer: { active: { type: 'alternate' }, onBufferChange() {} },
    element: { classList: { toggle() {} } },
    rows: 24,
    scrollLines() {},
  };
}
const touch = (x, y, target) => ({ clientX: x, clientY: y });
const sleep = ms => new Promise(r => setTimeout(r, ms));

eval(fnSrc);

(async () => {
  let failures = 0;
  const check = (name, cond) => { if (!cond) { failures++; console.error('FAIL', name); } else console.log('ok', name); };

  // 1) 干净 tap + touchend 被 xterm 抑制 → 恰好合成一次 mousedown/up
  {
    const el = makeEl(), x = makeXterm();
    const dispatched = [];
    const target = { dispatchEvent(e) { dispatched.push(e.type + ':' + e.buttons); } };
    enableTermTouchScroll(el, x);
    el.fire('touchstart', { touches: [touch(100, 200, target)], target });
    const end = { changedTouches: [touch(100, 200, target)], defaultPrevented: true };
    el.fire('touchend', end);
    await sleep(50);
    check('clean prevented tap synthesizes once', JSON.stringify(dispatched) === '["mousedown:1","mouseup:0"]');
  }
  // 2) touchend 未被抑制(浏览器会自行派发) → 不合成
  {
    const el = makeEl(), x = makeXterm();
    const dispatched = [];
    const target = { dispatchEvent(e) { dispatched.push(e.type); } };
    enableTermTouchScroll(el, x);
    el.fire('touchstart', { touches: [touch(100, 200, target)], target });
    el.fire('touchend', { changedTouches: [touch(100, 200, target)], defaultPrevented: false });
    await sleep(50);
    check('unprevented tap not synthesized', dispatched.length === 0);
  }
  // 3) 小拖动已派发 wheel → touchend 不合成(幽灵点击防护)
  {
    const el = makeEl(), x = makeXterm();
    const dispatched = [];
    const target = { dispatchEvent(e) { dispatched.push(e.type); } };
    enableTermTouchScroll(el, x);
    el.fire('touchstart', { touches: [touch(100, 200, target)], target });
    el.fire('touchmove', { touches: [touch(100, 192, target)], preventDefault() {} }); // 8px→派发 wheel
    el.fire('touchend', { changedTouches: [touch(100, 192, target)], defaultPrevented: true });
    await sleep(50);
    check('wheel-dispatched drag not synthesized', dispatched.length === 0);
  }
  // 4) 目标已离开终端(终端销毁/抽屉覆盖) → 不派发
  {
    const el = makeEl(), x = makeXterm();
    const dispatched = [];
    const target = { dispatchEvent(e) { dispatched.push(e.type); } };
    enableTermTouchScroll(el, x);
    el._targets = new Set(); // contains() 返回 false
    el.fire('touchstart', { touches: [touch(100, 200, target)], target });
    el.fire('touchend', { changedTouches: [touch(100, 200, target)], defaultPrevented: true });
    await sleep(50);
    check('detached target not dispatched', dispatched.length === 0);
  }
  // 5) 多终端实例互不抑制
  {
    const el1 = makeEl(), el2 = makeEl(), x1 = makeXterm(), x2 = makeXterm();
    const d1 = [], d2 = [];
    const t1 = { dispatchEvent(e) { d1.push(e.type); } };
    const t2 = { dispatchEvent(e) { d2.push(e.type); } };
    enableTermTouchScroll(el1, x1);
    enableTermTouchScroll(el2, x2);
    el1.fire('touchstart', { touches: [touch(10, 10, t1)], target: t1 });
    el1.fire('touchend', { changedTouches: [touch(10, 10, t1)], defaultPrevented: true });
    el2.fire('touchstart', { touches: [touch(20, 20, t2)], target: t2 });
    el2.fire('touchend', { changedTouches: [touch(20, 20, t2)], defaultPrevented: true });
    await sleep(50);
    check('two terminals both synthesize independently', d1.length === 2 && d2.length === 2);
  }
  process.exit(failures ? 1 : 0);
})();
"""


def _run_harness():
    harness = ROOT / "tests" / "_h5_tap_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    try:
        return subprocess.run(
            ["node", str(harness), str(ROOT / "static" / "index.html")],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        harness.unlink(missing_ok=True)


def test_h5_tap_synthesis_behavior():
    result = _run_harness()
    print(result.stdout)
    print(result.stderr)
    assert result.returncode == 0, result.stderr or result.stdout
