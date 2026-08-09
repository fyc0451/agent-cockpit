"""tests/test_term_fullscreen.py — 终端真全屏的 Node 行为测试(复审 #1537)。

从 static/index.html 抽取 toggleTermFullscreen/exitTermFullscreen 真实源码,
用桩 DOM 驱动:requestFullscreen 成功、reject 回退、退出与类清理。
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const src = html.match(/<script>([\s\S]*)<\/script>/)[1];

function extract(name) {
  const start = src.indexOf('function ' + name + '(');
  let i = src.indexOf('{', start), depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('extract failed: ' + name);
}

const stage = {
  classes: new Set(),
  classList: {
    add(c) { stage.classes.add(c); },
    remove(c) { stage.classes.delete(c); },
    contains(c) { return stage.classes.has(c); },
  },
  requestFullscreen: null,
  exited: 0,
};
const doc = { fullscreenElement: null };
globalThis.document = {
  getElementById: id => (id === 'termStage' ? stage : null),
  exitFullscreen() { doc.fullscreenElement = null; return Promise.resolve(); },
};
Object.defineProperty(globalThis.document, 'fullscreenElement', {
  get() { return doc.fullscreenElement; },
});

eval(extract('toggleTermFullscreen'));
eval(extract('exitTermFullscreen'));

(async () => {
  let failures = 0;
  const check = (name, cond) => { if (!cond) { failures++; console.error('FAIL', name); } else console.log('ok', name); };

  // 1) requestFullscreen 成功:不加 fixed 类
  {
    stage.requestFullscreen = () => { doc.fullscreenElement = stage; return Promise.resolve(); };
    toggleTermFullscreen();
    await Promise.resolve();
    check('native success keeps fixed class off', !stage.classList.contains('term-fs-fixed'));
    // 2) 经 toggle 退出: 调 exitFullscreen 且清理类
    toggleTermFullscreen();
    await Promise.resolve();
    check('toggle exit calls exitFullscreen', doc.fullscreenElement === null);
    check('toggle exit keeps classes clean', stage.classes.size === 0);
  }
  // 3) requestFullscreen reject → fixed 回退
  {
    doc.fullscreenElement = null;
    stage.requestFullscreen = () => Promise.reject(new Error('denied'));
    toggleTermFullscreen();
    await new Promise(r => setTimeout(r, 5));
    check('reject falls back to fixed', stage.classList.contains('term-fs-fixed'));
    // 4) fixed 模式下退出: 清类;fullscreenElement 非 stage 时不调 exitFullscreen
    let exitCalled = 0;
    const origExit = globalThis.document.exitFullscreen;
    globalThis.document.exitFullscreen = () => { exitCalled++; return Promise.resolve(); };
    exitTermFullscreen();
    check('fixed exit removes class', !stage.classList.contains('term-fs-fixed'));
    check('fixed exit does not call exitFullscreen', exitCalled === 0);
    globalThis.document.exitFullscreen = origExit;
  }
  // 5) 无 requestFullscreen(iOS) → 直接 fixed
  {
    stage.classes.clear();
    stage.requestFullscreen = undefined;
    toggleTermFullscreen();
    check('no API goes straight to fixed', stage.classList.contains('term-fs-fixed'));
    exitTermFullscreen();
    check('cleanup after no-API path', stage.classes.size === 0);
  }
  process.exit(failures ? 1 : 0);
})();
"""


def test_term_fullscreen_behavior(tmp_path):
    harness = tmp_path / "_term_fs_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness), str(ROOT / "static" / "index.html")],
        capture_output=True, text=True, timeout=60,
    )
    print(result.stdout, result.stderr)
    assert result.returncode == 0, result.stderr or result.stdout
