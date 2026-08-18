import { describe, expect, it } from 'vitest'
import {
  clampTermFontSize,
  loadTermFontSize,
  saveTermFontSize,
  TERM_FONT_DEFAULT,
  TERM_FONT_STORAGE_KEY,
} from '../features/terminal/termFont'
import {
  applyTermModifiers,
  encodeTermKey,
  EMPTY_MODIFIERS,
  isTermFocusReport,
  TERM_KEY_SEQ,
} from '../features/terminal/termKeys'
import {
  COMPOSE_MAX_PANES,
  defaultComposePicks,
  groupTabId,
  layoutGroup,
  panesForSession,
  pickLayoutTarget,
  pickPairIds,
} from '../features/terminal/termLayout'

describe('termKeys 1.0 按键序列', () => {
  it('方向键和 Ctrl-C/Ctrl-B 与 1.0 一致', () => {
    expect(TERM_KEY_SEQ.ArrowUp).toBe('\x1b[A')
    expect(TERM_KEY_SEQ.Enter).toBe('\r')
    expect(encodeTermKey('CtrlC', EMPTY_MODIFIERS)?.seq).toBe('\x03')
    expect(encodeTermKey('CtrlB', EMPTY_MODIFIERS)?.seq).toBe('\x02')
  })

  it('Ctrl+方向键生成 CSI 1;5 序列并清修饰键', () => {
    const result = applyTermModifiers('\x1b[A', 'ArrowUp', { ctrl: true, alt: false, shift: false })
    expect(result.seq).toBe('\x1b[1;5A')
    expect(result.mods).toEqual(EMPTY_MODIFIERS)
  })

  it('丢掉 DECSET 1004 焦点报告', () => {
    expect(isTermFocusReport('\x1b[I')).toBe(true)
    expect(isTermFocusReport('\x1b[O')).toBe(true)
    expect(isTermFocusReport('\x1b[A')).toBe(false)
  })
})

describe('termLayout 快捷布局', () => {
  const panes = [
    { pane_id: 'w1:p1', session: 'cockpit', agent: 'grok', tab_id: 't1', focused: true },
    { pane_id: 'w1:p2', session: 'cockpit', agent: 'codex', tab_id: 't1' },
    { pane_id: 'w1:p9', session: 'other', agent: 'kimi', tab_id: 't9' },
  ]

  it('只取当前 session 的 pane，并配对两个 Agent', () => {
    const local = panesForSession(panes, 'cockpit')
    expect(local.map((pane) => pane.pane_id)).toEqual(['w1:p1', 'w1:p2'])
    const target = pickLayoutTarget(local)
    expect(target?.pane_id).toBe('w1:p1')
    expect(pickPairIds(local, target)).toEqual(['w1:p1', 'w1:p2'])
    expect(groupTabId(target, local)).toBe('t1')
  })

  it('快捷 pair 仍只取两个；组合默认勾选当前组或最多 4 个 Agent', () => {
    const many = [
      { pane_id: 'w1:p1', session: 'cockpit', agent: 'grok', tab_id: 't1', focused: true },
      { pane_id: 'w1:p2', session: 'cockpit', agent: 'codex', tab_id: 't1' },
      { pane_id: 'w1:p3', session: 'cockpit', agent: 'claude', tab_id: 't1' },
      { pane_id: 'w1:p4', session: 'cockpit', agent: 'opencode', tab_id: 't2' },
      { pane_id: 'w1:p5', session: 'cockpit', agent: 'kimi', tab_id: 't2' },
      { pane_id: 'w1:p6', session: 'cockpit', agent: '', tab_id: 't1' },
    ]
    const target = pickLayoutTarget(many)
    expect(pickPairIds(many, target)).toEqual(['w1:p1', 'w1:p2'])
    const { tabId, group } = layoutGroup(target, many)
    expect(tabId).toBe('t1')
    expect(group.map((pane) => pane.pane_id)).toEqual(['w1:p1', 'w1:p2', 'w1:p3', 'w1:p6'])
    expect(defaultComposePicks(many, target, group)).toEqual(['w1:p1', 'w1:p2', 'w1:p3'])

    const singles = many.map((pane) => ({ ...pane, tab_id: pane.pane_id }))
    const lonely = pickLayoutTarget(singles)
    const fallback = layoutGroup(lonely, singles)
    expect(fallback.group).toHaveLength(1)
    const picks = defaultComposePicks(singles, lonely, fallback.group)
    expect(picks).toEqual(['w1:p1', 'w1:p2', 'w1:p3', 'w1:p4'])
    expect(picks).toHaveLength(COMPOSE_MAX_PANES)
    expect(picks).not.toContain('w1:p6')
  })

  it('焦点在单 pane tab 时，拆开整组回退到最大多分屏组', () => {
    const mixed = [
      { pane_id: 'w1:p1', session: 'cockpit', agent: 'grok', tab_id: 't9', focused: true },
      { pane_id: 'w1:p2', session: 'cockpit', agent: 'codex', tab_id: 't5' },
      { pane_id: 'w1:p3', session: 'cockpit', agent: 'claude', tab_id: 't5' },
      { pane_id: 'w1:p4', session: 'cockpit', agent: 'opencode', tab_id: 't5' },
    ]
    const target = pickLayoutTarget(mixed)
    expect(target?.pane_id).toBe('w1:p1')
    const { tabId, group } = layoutGroup(target, mixed)
    expect(tabId).toBe('t5')
    expect(group).toHaveLength(3)
    expect(groupTabId(target, mixed)).toBe('t5')
  })
})

describe('termFont 1.0 本机字号', () => {
  afterEach(() => {
    window.localStorage.removeItem(TERM_FONT_STORAGE_KEY)
  })

  it('夹在 10–24，默认 13', () => {
    expect(clampTermFontSize(9)).toBe(10)
    expect(clampTermFontSize(25)).toBe(24)
    expect(clampTermFontSize('nope')).toBe(TERM_FONT_DEFAULT)
    expect(loadTermFontSize()).toBe(TERM_FONT_DEFAULT)
  })

  it('写入 localStorage 后立刻读回', () => {
    expect(saveTermFontSize(18)).toBe(18)
    expect(window.localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe('18')
    expect(loadTermFontSize()).toBe(18)
  })
})
