// 群聊工作台纯逻辑单测：leader 持久化、@ 解析（含 @leader 别名）、摘要 diff、会话归属。

import { mailBelongsToSession } from '../api/chatSession'
import { isAlreadyStoppedError } from '../api/legacyHerdr'
import { ApiError } from '../api/client'
import { computeColumns, shouldOverlayDetails } from '../features/shell/columns'
import type { HerdrPane } from '../api/legacyHerdr'
import {
  AGENT_KINDS,
  buildLaunchArgs,
  buildSessionRows,
  canRecallEntry,
  isHumanSender,
  isMemberRosterEvent,
  mailCoversLocalMe,
  mailTimestamp,
  mailToEntries,
  diffSummaryLines,
  groupByLedger,
  leftoverMemberName,
  loadComposerDraft,
  saveComposerDraft,
  nextSessionAfterRemoval,
  shouldAdoptUrlSession,
  memberName,
  membersOfSession,
  mentionQueryAt,
  parseMentionTargets,
  recallNotice,
  rootBase,
  sessionRoot,
  shouldSendOnEnter,
  splitMessageParts,
  composerPreviewLabel,
  isIdentityChromeOnly,
  layoutMessageBlocks,
  messageFoldPreview,
  messageNeedsFold,
  reflowMessageText,
  restoreBoxTables,
  splitInlineMarks,
  stripAgentTuiFooter,
  stripMailMeta,
  agentReplyTargets,
  appendAttachMarkup,
  attachMarkup,
  clipboardImageFile,
  COMPOSER_SKILLS,
  mailSkillInsert,
  shouldAnnounceMemberChange,
  shouldSeedMemberRoster,
  statusMeta,
  typingEntries,
  withLeader,
  type ChatMember,
  type SessionRow,
} from '../features/group-chat/model'

function pane(over: Partial<HerdrPane>): HerdrPane {
  return {
    pane_id: '%1',
    session: 's1',
    agent: 'codex',
    agent_status: 'idle',
    cwd: '/repo/p1',
    cwd_name: 'p1',
    display_name: '',
    mail_name: '',
    tab_id: '',
    focused: false,
    ...over,
  }
}

function member(over: Partial<ChatMember>): ChatMember {
  return {
    paneId: '%1',
    session: 's1',
    kind: 'codex',
    name: 'codex-01',
    mailName: 'codex-01',
    status: 'idle',
    cwd: '/repo/p1',
    isLeader: false,
    ...over,
  }
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('session isolation / mobile details', () => {
  it('别的 session thread 不能进本群', () => {
    expect(mailBelongsToSession('cockpit', 'cockpit')).toBe(true)
    expect(mailBelongsToSession('th_abc', 'platform')).toBe(false)
    expect(mailBelongsToSession('', 'platform')).toBe(false)
    expect(mailBelongsToSession('cockpit', 'pitapat-video-platform-1')).toBe(false)
    expect(leftoverMemberName('kimi-main', 'kimi')).toBe(true)
    expect(leftoverMemberName('FoggyBasin', 'kimi')).toBe(false)
    expect(statusMeta('done').label).toBe('空闲')
    expect(membersOfSession({
      panes: [pane({ session: 's1', pane_id: 'w1:p5', agent: 'kimi', agent_status: 'done' })],
    }, 's1')[0].status).toBe('idle')
    expect(memberName(pane({
      display_name: 'kimi',
      mail_name: 'FoggyBasin',
      agent: 'kimi',
    }))).toBe('FoggyBasin')
    expect(shouldAnnounceMemberChange('cockpit', 'pitapat-video-platform-1')).toBe(false)
    expect(shouldAnnounceMemberChange('pitapat-video-platform-1', 'pitapat-video-platform-1')).toBe(true)
    expect(shouldAnnounceMemberChange(null, 'pitapat-video-platform-1')).toBe(false)
    expect(shouldSeedMemberRoster('cockpit', 'cockpit', 0)).toBe(true)
    expect(shouldSeedMemberRoster('cockpit', 'cockpit', 3)).toBe(false)
    expect(shouldSeedMemberRoster(null, 'cockpit', 3)).toBe(true)
    expect(isMemberRosterEvent('FoggyBasin 加入了群聊')).toBe(true)
    expect(isMemberRosterEvent('BrownDesert 离开了群聊')).toBe(true)
    expect(isMemberRosterEvent('会话已恢复')).toBe(false)
  })

  it('窄屏 details 走覆盖层，不会被让位链关掉', () => {
    expect(shouldOverlayDetails(390)).toBe(true)
    expect(shouldOverlayDetails(1200)).toBe(false)
    expect(computeColumns(390, 0, 0).sidebar).toBe(0)
    expect(computeColumns(1440, 0, 0).sidebar).toBe(0)
  })

  it('附件写入正文路径，剪贴板图片可取出', () => {
    expect(attachMarkup('a.png', '/tmp/a.png')).toBe('📎 a.png\n/tmp/a.png')
    expect(appendAttachMarkup('你好', 'a.png', '/tmp/a.png')).toBe('你好\n\n📎 a.png\n/tmp/a.png\n')
    expect(COMPOSER_SKILLS.map((item) => item.id)).toEqual(['herdr', 'mail'])
    expect(mailSkillInsert('scc-1')).toContain('--thread scc-1')
    expect(mailSkillInsert('scc-1')).not.toContain('--thread cockpit')
    const image = new File(['x'], 'shot.png', { type: 'image/png' })
    expect(clipboardImageFile([
      { type: 'text/plain', getAsFile: () => null },
      { type: 'image/png', getAsFile: () => image },
    ])).toBe(image)
    expect(clipboardImageFile([{ type: 'text/plain', getAsFile: () => null }])).toBeNull()
  })
})

describe('memberName / membersOfSession', () => {
  it('优先 display_name，其次 mail_name，兜底 kind+pane 尾号；过滤非 agent pane', () => {
    expect(memberName(pane({ display_name: '小克' }))).toBe('小克')
    expect(memberName(pane({ mail_name: 'claude-3' }))).toBe('claude-3')
    expect(memberName(pane({ pane_id: '%42', agent: 'kimi' }))).toBe('kimi-42')
    expect(memberName(pane({
      agent: 'codex',
      display_name: 'codex',
      mail_name: 'codex-main',
      pane_id: 'w1:p3',
    }))).toBe('codex-main')

    const snap = {
      panes: [pane({ pane_id: '%1' }), pane({ pane_id: '%2', agent: '' })],
    }
    const ms = membersOfSession(snap, 's1')
    expect(ms).toHaveLength(1)
    expect(ms[0].paneId).toBe('%1')
  })

  it('重名成员自动加 · 去重', () => {
    const snap = {
      panes: [
        pane({ pane_id: '%1', display_name: '小克' }),
        pane({ pane_id: '%2', display_name: '小克' }),
      ],
    }
    const names = membersOfSession(snap, 's1').map((m) => m.name)
    expect(names).toEqual(['小克', '小克·'])
  })
})

describe('withLeader', () => {
  it('第一个成员默认 leader 并持久化；pane 消失后重算', () => {
    const a = member({ paneId: '%1', name: 'codex-01' })
    const b = member({ paneId: '%2', name: 'claude-02', kind: 'claude' })
    const first = withLeader('sA', [a, b])
    expect(first.find((m) => m.isLeader)?.paneId).toBe('%1')
    // 持久化后换顺序仍认同一个
    const second = withLeader('sA', [b, a])
    expect(second.find((m) => m.isLeader)?.paneId).toBe('%1')
    // leader pane 离开 → 重算为现存第一个
    const third = withLeader('sA', [b])
    expect(third.find((m) => m.isLeader)?.paneId).toBe('%2')
  })
})

describe('mentionQueryAt', () => {
  it('光标在 @ 后进入补全；@ 前必须是行首/空白', () => {
    expect(mentionQueryAt('@le', 3)).toEqual({ start: 0, query: 'le' })
    expect(mentionQueryAt('hi @le', 6)).toEqual({ start: 3, query: 'le' })
    expect(mentionQueryAt('a@b', 3)).toBeNull()
    expect(mentionQueryAt('@a\n@b', 5)).toEqual({ start: 3, query: 'b' })
  })
})

describe('parseMentionTargets', () => {
  const leader = member({ paneId: '%1', name: '小克', kind: 'claude', isLeader: true })
  const dev = member({ paneId: '%2', name: 'codex-02', kind: 'codex' })

  it('@花名 精确命中；@类型 命中该类型全部；去重', () => {
    expect(parseMentionTargets('@小克 看下', [leader, dev])).toEqual([leader])
    expect(parseMentionTargets('@codex 干活', [leader, dev])).toEqual([dev])
    expect(parseMentionTargets('@小克 @小克', [leader, dev])).toEqual([leader])
  })

  it('@leader 别名 → 带徽章的成员（大小写不敏感）', () => {
    expect(parseMentionTargets('@leader 分派任务', [leader, dev])).toEqual([leader])
    expect(parseMentionTargets('@Leader 分派任务', [leader, dev])).toEqual([leader])
  })

  it('无命中 → 空数组（调用方回退默认 leader）', () => {
    expect(parseMentionTargets('随便说一句', [leader, dev])).toEqual([])
  })

  it('@kimi-main 命中唯一在场的 kimi 花名', () => {
    const kimi = member({ paneId: '%5', name: 'FoggyBasin', kind: 'kimi', mailName: 'FoggyBasin' })
    expect(parseMentionTargets('@kimi-main 看下', [leader, kimi])).toEqual([kimi])
    expect(parseMentionTargets('@kimi-agent-cockpit 看下', [leader, kimi])).toEqual([kimi])
  })

  it('裸 @codex 不打已有花名的 Codex，避免串到别的群的任务', () => {
    const flower = member({
      paneId: 'w1:p2', name: 'EmeraldCave', kind: 'codex', mailName: 'EmeraldCave',
    })
    expect(parseMentionTargets('@codex 做15轮', [leader, flower])).toEqual([])
    expect(parseMentionTargets('@EmeraldCave 做15轮', [leader, flower])).toEqual([flower])
    const leftover = member({
      paneId: 'w1:p3', name: 'codex-main', kind: 'codex', mailName: 'codex-main',
    })
    expect(parseMentionTargets('@codex 做15轮', [leader, leftover])).toEqual([leftover])
  })
})

describe('diffSummaryLines', () => {
  it('尾部追加 → 只返回新增行', () => {
    expect(diffSummaryLines('a\nb', 'a\nb\nc')).toBe('c')
  })
  it('无重叠（清空/轮转）→ 返回全文；无变化 → 空串', () => {
    expect(diffSummaryLines('a\nb', 'x\ny')).toBe('x\ny')
    expect(diffSummaryLines('a\nb', 'a\nb')).toBe('')
    expect(diffSummaryLines('', 'a')).toBe('a')
  })
  it('摘要窗口滑动：尾部新增只回新行；纯截断无新增 → 空串', () => {
    // pane summary 是尾部窗口：新行到来时旧行从头部挤出
    expect(diffSummaryLines('a\nb\nc', 'b\nc\nd')).toBe('d')
    expect(diffSummaryLines('a\nb\nc', 'b\nc')).toBe('')
  })
})

describe('sessionRoot / rootBase / buildSessionRows', () => {
  const roots = ['/repo/p1', '/repo/p2']

  it('rootBase 取 basename，容忍尾斜杠', () => {
    expect(rootBase('/repo/p1')).toBe('p1')
    expect(rootBase('/repo/p1/')).toBe('p1')
  })

  it('按 agent pane cwd 匹配最长前缀 root；无匹配 → null', () => {
    const panes = [
      pane({ session: 's1', cwd: '/repo/p1/sub' }),
      pane({ session: 's2', cwd: '/elsewhere' }),
    ]
    expect(sessionRoot(panes, 's1', roots)).toBe('/repo/p1')
    expect(sessionRoot(panes, 's2', roots)).toBeNull()
    expect(sessionRoot(panes, 's1', [])).toBeNull()
  })

  it('buildSessionRows 收 running 和 stopped；running 聚合 blocked 优先', () => {
    const sessions = [
      { name: 's1', status: 'running', directory: '', socket: '' },
      { name: 's2', status: 'stopped', directory: '', socket: '' },
    ]
    const snap = {
      panes: [
        pane({ pane_id: '%1', session: 's1', agent_status: 'idle' }),
        pane({ pane_id: '%2', session: 's1', agent: 'kimi', agent_status: 'blocked' }),
      ],
    }
    const rows = buildSessionRows(sessions, snap, roots)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ name: 's1', status: 'blocked', memberCount: 2, root: '/repo/p1' })
    expect(rows[1]).toMatchObject({ name: 's2', status: 'stopped', memberCount: 0 })
  })

  it('stopped 会话用 snapshot 里的 descriptor pane 计人数', () => {
    const sessions = [{ name: 's2', status: 'stopped', directory: '', socket: '' }]
    const snap = {
      panes: [
        pane({ pane_id: 'w1:p9', session: 's2', agent: 'codex', agent_status: 'stopped' }),
        pane({ pane_id: 'w1:p1', session: 's2', agent: 'grok', agent_status: 'stopped' }),
      ],
    }
    const rows = buildSessionRows(sessions, snap)
    expect(rows[0]).toMatchObject({ name: 's2', status: 'stopped', memberCount: 2 })
    expect(membersOfSession(snap, 's2').map((m) => m.kind)).toEqual(['codex', 'grok'])
  })
})

describe('splitMessageParts', () => {
  it('无围栏则整段是文本；围栏代码单独成块且默认不折叠', () => {
    expect(splitMessageParts('hello')).toEqual([{ type: 'text', text: 'hello' }])
    expect(splitMessageParts('看这段\n```ts\nconst n = 1\n```\n完')).toEqual([
      { type: 'text', text: '看这段\n' },
      { type: 'code', lang: 'ts', text: 'const n = 1' },
      { type: 'text', text: '\n完' },
    ])
  })
})

describe('reflow and fold long waterfall text', () => {
  const wall = (
    '当前 master 已是最新。一、平台/Gateway：使用新路径 GET /v1/video-generations/health。'
    + '二、平台镜像：docker compose up -d --build。三、GPU 主机：sudo ./gpu_service/deploy.sh。'
    + '\n› Improve documentation in @filename'
    + '\ngpt-5.6-sol high · Full Access · Context 63% left · Fast off · Ready · pitapat-video-platform'
  )

  it('拆开一、二、三和命令，并去掉 TUI 脚', () => {
    const text = reflowMessageText(wall)
    expect(text).toContain('\n\n一、')
    expect(text).toContain('\n\n二、')
    expect(text).toContain('\nGET /v1/video-generations/health')
    expect(text).not.toContain('Full Access')
    expect(stripAgentTuiFooter(wall)).not.toContain('› Improve')
  })

  it('长文默认折叠，短文不折叠', () => {
    const long = Array.from(
      { length: 20 },
      (_, index) => `${index + 1}. 第 ${index + 1} 段还要再写一些说明，避免瀑布流只剩尾巴。`,
    ).join('\n')
    expect(messageNeedsFold(long)).toBe(true)
    expect(messageNeedsFold('好的')).toBe(false)
    expect(messageFoldPreview(long).split('\n').length).toBeLessThanOrEqual(12)
    expect(composerPreviewLabel(wall)).toMatch(/…$/)
    expect(composerPreviewLabel('短草稿')).toBe('短草稿')
    expect(composerPreviewLabel('')).toBe('写消息')
  })

  it('对照终端：挤成一段的发布说明拆成标题、命令和列表', () => {
    const wall = (
      '当前 master 已是最新 e0cdd8c，工作区干净。具体修改分三部分：'
      + '一、平台/Gateway：使用新路径 GET /v1/video-generations/health。'
      + '跑 uv run pytest -q tests/gpu_service tests/test_gpu_bridge.py，当前 113 passed。'
      + '二、平台镜像：在仓库根目录执行 docker compose up -d --build。'
      + '三、GPU 主机：在 GPU 主机执行 sudo ./gpu_service/deploy.sh。'
    )
    const types = layoutMessageBlocks(wall).map((block) => block.type)
    expect(types).toContain('heading')
    expect(types).toContain('code')
    expect(layoutMessageBlocks(wall).some((block) => (
      block.type === 'heading' && block.text.includes('一、')
    ))).toBe(true)
    const listed = layoutMessageBlocks(
      'GPU Service 部署并通过 smoke\n-> 验证 health/create/query\n-> 重建并发布 Gateway 镜像',
    )
    expect(listed.some((block) => block.type === 'list' && block.items.length === 2)).toBe(true)
    const table = layoutMessageBlocks(
      '| 产物 | 节点 | 写入缓存 |\n| --- | --- | --- |\n| 角色核心 | C007 | mbti_profile |',
    )
    expect(table).toEqual([{
      type: 'table',
      headers: ['产物', '节点', '写入缓存'],
      rows: [['角色核心', 'C007', 'mbti_profile']],
    }])
    expect(splitInlineMarks('用 `client.py` 和 **不要** 旧接口')).toEqual([
      { type: 'text', text: '用 ' },
      { type: 'code', text: 'client.py' },
      { type: 'text', text: ' 和 ' },
      { type: 'strong', text: '不要' },
      { type: 'text', text: ' 旧接口' },
    ])
    expect(splitInlineMarks('只吃 ${U182_PACK.role_core}')).toEqual([
      { type: 'text', text: '只吃 ' },
      { type: 'code', text: '${U182_PACK.role_core}' },
    ])
    const boxed = restoreBoxTables(
      '┌──┬──┐\n│ 产物 │ 节点 │\n├──┼──┤\n│ 角色核心 │ C007 │\n└──┴──┘',
    )
    expect(boxed).toContain('| 产物 | 节点 |')
    expect(boxed).toContain('| 角色核心 | C007 |')
    const fromTui = layoutMessageBlocks(
      '正式画像：流程自己总结\n'
      + '     • fixedSummary.roleProfile.content → rag_role_persona\n'
      + '角色 LLM 实际读什么 N1778141506217 只吃：',
    )
    expect(fromTui.some((block) => block.type === 'heading' && block.text.includes('正式画像'))).toBe(true)
    expect(fromTui.some((block) => block.type === 'list')).toBe(true)
    expect(layoutMessageBlocks('注册:花名=codex-luna-agent-cockpit,项\n作。').some((block) => block.type === 'heading')).toBe(false)
    expect(layoutMessageBlocks('撤回').some((block) => block.type === 'heading')).toBe(false)
  })
})

describe('groupByLedger', () => {
  const row = (name: string, root: string | null = null): SessionRow => ({
    name,
    status: 'idle',
    memberCount: 1,
    root,
  })

  it('按 thread 挂到工作区；无 thread 进未分组；已登记工作区都可删', () => {
    const { groups, ungrouped } = groupByLedger(
      [row('foo-1'), row('orphan')],
      [
        { id: 'ws_a', path: '/repo/foo', title: 'foo' },
        { id: 'ws_b', path: '/repo/bar', title: 'bar' },
      ],
      [{ workspace_id: 'ws_a', herdr_session: 'foo-1' }],
    )
    expect(groups).toHaveLength(2)
    expect(groups[0]).toMatchObject({
      id: 'ws_a',
      root: '/repo/foo',
      label: 'foo',
      removable: true,
    })
    expect(groups[0].rows.map((r) => r.name)).toEqual(['foo-1'])
    expect(groups[0].rows[0].root).toBe('/repo/foo')
    expect(groups[1].rows).toEqual([])
    expect(ungrouped.map((r) => r.name)).toEqual(['orphan'])
    expect(ungrouped[0].root).toBeNull()
  })

  it('已停/不可达的 herdr stop 错误应继续删除', () => {
    expect(
      isAlreadyStoppedError(
        new ApiError({
          code: 'herdr_error',
          message:
            'herdr session stop demo 失败: {"error":{"code":"session_stop_failed","message":"session demo is not running or cannot be reached at /tmp/herdr.sock"}}',
          retryable: false,
        }),
      ),
    ).toBe(true)
    expect(isAlreadyStoppedError(new ApiError({ code: 'herdr_error', message: 'busy', retryable: false }))).toBe(false)
  })

  it('herdr 已消失的账本 thread 仍显示为已停止，方便再删', () => {
    const { groups, ungrouped } = groupByLedger(
      [],
      [{ id: 'ws_a', path: '/repo/foo', title: 'foo' }],
      [{ workspace_id: 'ws_a', herdr_session: 'pitapat-pitpat-emotion-v2-1-0' }],
    )
    expect(ungrouped).toEqual([])
    expect(groups[0].rows).toEqual([
      {
        name: 'pitapat-pitpat-emotion-v2-1-0',
        status: 'stopped',
        memberCount: 0,
        root: '/repo/foo',
      },
    ])
  })

  it('删掉工作区后 thread 对应会话掉进未分组', () => {
    const { groups, ungrouped } = groupByLedger(
      [row('foo-1')],
      [],
      [{ workspace_id: 'ws_gone', herdr_session: 'foo-1' }],
    )
    expect(groups).toEqual([])
    expect(ungrouped.map((r) => r.name)).toEqual(['foo-1'])
  })
})

describe('删除会话后的选中回落', () => {
  it('删当前会话落到下一条；删的不是当前则不动', () => {
    expect(nextSessionAfterRemoval(['a', 'b', 'c'], 'b', 'b')).toBe('a')
    expect(nextSessionAfterRemoval(['a', 'b'], 'a', 'a')).toBe('b')
    expect(nextSessionAfterRemoval(['only'], 'only', 'only')).toBeNull()
    expect(nextSessionAfterRemoval(['a', 'b'], 'b', 'a')).toBe('a')
  })

  it('已初始化且列表里没有 URL 会话时不回写，避免和清空互踢', () => {
    expect(shouldAdoptUrlSession('gone', null, ['keep'], true)).toBe(false)
    expect(shouldAdoptUrlSession('gone', 'gone', ['keep'], true)).toBe(false)
    expect(shouldAdoptUrlSession('gone', null, [], true)).toBe(false)
    expect(shouldAdoptUrlSession('keep', null, ['keep'], true)).toBe(true)
    expect(shouldAdoptUrlSession('maybe', null, [], false)).toBe(true)
  })
})

describe('AGENT_KINDS 规格冻结', () => {
  it('只有 codex / claude / kimi / opencode / grok', () => {
    expect([...AGENT_KINDS]).toEqual(['codex', 'claude', 'kimi', 'opencode', 'grok'])
  })
})

describe('launch / recall helpers', () => {
  it('kimi 权限和模型拼进启动参数', () => {
    expect(buildLaunchArgs('kimi', 'kimi-code/k3', 'yolo')).toBe('-m kimi-code/k3 -y')
    expect(buildLaunchArgs('kimi', '', 'auto')).toBe('--auto')
    expect(buildLaunchArgs('codex', 'gpt-5', 'yolo')).toBe('-m gpt-5')
  })

  it('组字中或 IME 229 回车不发送', () => {
    expect(shouldSendOnEnter({ key: 'Enter', shiftKey: false })).toBe(true)
    expect(shouldSendOnEnter({ key: 'Enter', shiftKey: true })).toBe(false)
    expect(shouldSendOnEnter({ key: 'Enter', shiftKey: false, isComposing: true })).toBe(false)
    expect(shouldSendOnEnter({ key: 'Enter', shiftKey: false, keyCode: 229 })).toBe(false)
  })

  it('输入草稿按会话记在 sessionStorage，登录卸页也能读回', () => {
    saveComposerDraft('cockpit', '还没发出去的话')
    expect(loadComposerDraft('cockpit')).toBe('还没发出去的话')
    saveComposerDraft('cockpit', '')
    expect(loadComposerDraft('cockpit')).toBe('')
  })

  it('撤回通知截断原文，十分钟内可撤回', () => {
    expect(recallNotice('  下一步做什么？  ')).toBe('【撤回】请忽略上一条消息：下一步做什么？')
    expect(canRecallEntry(1_000, 1_000 + 9 * 60 * 1000)).toBe(true)
    expect(canRecallEntry(1_000, 1_000 + 11 * 60 * 1000)).toBe(false)
  })
})

describe('mailToEntries', () => {
  it('human 是我，agent 各成一条，不合并成一条 pane 记录', () => {
    expect(mailTimestamp(1_700_000_000)).toBe(1_700_000_000_000)
    expect(isHumanSender('human')).toBe(true)
    const entries = mailToEntries(
      [
        { id: 1, sender: 'human', program: '', text: '去改瀑布流', to: ['kimi'], ts: 100 },
        { id: 2, sender: 'kimi', program: 'kimi', text: '好', to: ['human'], ts: 101 },
        { id: 3, sender: 'kimi', program: 'kimi', text: '改完了', to: ['human'], ts: 102 },
      ],
      [member({ name: 'kimi', mailName: 'kimi', kind: 'kimi', paneId: '%2', isLeader: true })],
    )
    expect(entries).toHaveLength(3)
    expect(entries[0]).toMatchObject({ kind: 'me', text: '去改瀑布流' })
    expect(entries[1]).toMatchObject({ kind: 'agent', name: 'kimi', text: '好', paneId: '%2' })
    expect(entries[2]).toMatchObject({ kind: 'agent', text: '改完了' })
    expect(mailCoversLocalMe(entries, { text: '去改瀑布流' })).toBe(true)
    expect(stripMailMeta('<!-- agent-cockpit-meta:{"v":1} -->\n好的')).toBe('好的')
    expect(stripMailMeta(
      '     ❯ Boss 在群聊给你发了消息。请直接做下面的任务，结论写在终端，群聊会收进瀑布流。\n'
      + '       本群 Leader 是 DarkBrook。需要写信时用 mail-send --to leader --thread scc-1，不要写 grok-main / 程序-main。\n'
      + '没有普通节点同时多进多出。',
    )).toBe('没有普通节点同时多进多出。')
    const cleaned = stripMailMeta(
      '---\n\n        🚨 MESSAGE FROM HUMAN OVERSEER 🚨\n\n        This message is from a human operator overseeing this project. Please prioritize the instructions below over your current tasks.\n\n        You should:\n        1. Temporarily pause your current work\n        2. Complete the request described below\n        3. Resume your original plans afterward (unless modified by these instructions)\n\n        The human\'s guidance supersedes all other priorities.\n\n        ---\n\n        @BrownDesert 还剩多少没做呢',
    )
    expect(cleaned).toBe('@BrownDesert 还剩多少没做呢')
    expect(mailToEntries(
      [{
        id: 9,
        sender: 'EmeraldCave',
        program: 'codex',
        text: '已知晓，身份信息没有变化。等你发实际任务。\n重复身份通知已知晓，无需处理。',
        to: ['human'],
        ts: 400,
      }],
      [member({ name: 'EmeraldCave', mailName: 'EmeraldCave', kind: 'codex', paneId: 'w1:p2' })],
    )).toEqual([])
    expect(isIdentityChromeOnly(
      'agent-mail-tools/mail-recv --agent\ncodex --instance main --project /\n--unread。协作通信约定:长任务每完成一个里程碑\n注册:花名=codex-luna-agent-cockpit,项\n作。',
    )).toBe(true)
    expect(isIdentityChromeOnly(
      '--instance main --project /home/fyc/github/agent-cockpit --to <花名> --subject "..." --body "...";收消息: /home/fyc/github/agent-cockpit/agent-mail-tools/mail-recv --agent codex --instance',
    )).toBe(true)
    expect(mailToEntries(
      [{
        id: 10,
        sender: 'EmeraldCave',
        program: 'codex',
        text: 'agent-mail-tools/mail-recv --agent\ncodex --instance main --unread。协作通信约定:先 claim。\n注册:花名=codex-luna-agent-cockpit',
        to: ['human'],
        ts: 401,
      }],
      [member({ name: 'EmeraldCave', mailName: 'EmeraldCave', kind: 'codex', paneId: 'w1:p2' })],
    )).toEqual([])
    expect(isIdentityChromeOnly('瀑布流已经加大，请硬刷新。')).toBe(false)
    expect(isIdentityChromeOnly(
      '这不是在干活的 Codex。屏幕上的花名=codex-luna-agent-cockpit 是旧身份，不是任务。',
    )).toBe(false)
    expect(isHumanSender('HumanOverseer')).toBe(true)
    const live = mailToEntries(
      [{ id: 'pane:w1:p2', sender: 'BrownDesert', program: 'grok', text: '正在改瀑布流', to: ['human'], ts: 200 }],
      [member({ name: 'BrownDesert', mailName: 'BrownDesert', kind: 'grok', paneId: 'w1:p2' })],
    )
    expect(live[0]).toMatchObject({ id: 'pane:w1:p2', kind: 'agent', name: 'BrownDesert' })
    const members = [
      member({ name: 'BrownDesert', mailName: 'BrownDesert', kind: 'grok', paneId: 'w1:p1' }),
      member({ name: 'codex-main', mailName: 'codex-main', kind: 'codex', paneId: 'w1:p2' }),
    ]
    expect(agentReplyTargets(['codex-main', 'human'], members)).toEqual(['codex-main'])
    expect(agentReplyTargets(['human'], members)).toEqual([])
    const peer = mailToEntries(
      [{ id: 4, sender: 'BrownDesert', program: 'grok', text: '方案通过', to: ['codex-main'], ts: 300 }],
      members,
    )
    expect(peer[0]).toMatchObject({ kind: 'agent', name: 'BrownDesert', to: ['codex-main'] })
    const toBoss = mailToEntries(
      [{ id: 5, sender: 'BrownDesert', program: 'grok', text: '收到', to: ['human'], ts: 301 }],
      members,
    )
    expect(toBoss[0]).toMatchObject({ to: ['我'] })
    const mapped = mailToEntries(
      [
        { id: 6, sender: 'human', program: '', text: '看一下', to: ['kimi-main'], ts: 302 },
        { id: 7, sender: 'FoggyBasin', program: 'kimi', text: '看到了', to: ['human'], ts: 303 },
      ],
      [member({ name: 'FoggyBasin', mailName: 'FoggyBasin', kind: 'kimi', paneId: 'w1:p5' })],
    )
    expect(mapped[0]).toMatchObject({ kind: 'me', to: ['FoggyBasin'] })
    expect(mapped[1]).toMatchObject({ kind: 'agent', to: ['我'] })
  })

  it('工作中只占一条状态气泡，不是整屏 pane', () => {
    const rows = typingEntries([
      member({ name: 'BrownDesert', paneId: 'w1:p2', status: 'working' }),
      member({ name: 'kimi', paneId: 'w1:p3', status: 'idle' }),
      member({ name: 'SwiftFox', paneId: 'w1:p4', status: 'blocked' }),
    ], 9)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({
      id: 'typing:w1:p2',
      name: 'BrownDesert',
      text: '正在回复…',
    })
    expect(rows[1]).toMatchObject({
      id: 'typing:w1:p4',
      text: '需要你确认，点「看现场」处理',
    })
  })
})
