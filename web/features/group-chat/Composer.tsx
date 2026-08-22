// 输入卡片：@ 成员补全 + 附件/Skill + 粘贴截图 + 发送。

import { useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { IconSendOutline16 } from '../shell/icons'
import { fetchChatSkills, type ChatSkill } from '../../api/chatSession'
import {
  agentEmoji,
  clipboardImageFile,
  composerSkills,
  composerPreviewLabel,
  mentionQueryAt,
  messageNeedsFold,
  parseMentionTargets,
  shouldSendOnEnter,
  type ChatDelivery,
  type ChatMember,
} from './model'

interface ComposerProps {
  session?: string
  members: ChatMember[]
  leader: ChatMember | null
  value: string
  onChange: (v: string) => void
  onSend: (delivery: ChatDelivery) => void
  onAttach: (file: File) => void
  attaching?: boolean
  disabled: boolean
  inputRef?: MutableRefObject<HTMLTextAreaElement | null>
}

export function Composer({
  session = '',
  members,
  leader,
  value,
  onChange,
  onSend,
  onAttach,
  attaching = false,
  disabled,
  inputRef,
}: ComposerProps) {
  const innerRef = useRef<HTMLTextAreaElement | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = (el: HTMLTextAreaElement | null) => {
    innerRef.current = el
    if (inputRef) inputRef.current = el
  }
  const [mention, setMention] = useState<{ start: number; query: string } | null>(null)
  const [activeIdx, setActiveIdx] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const [fileAccept, setFileAccept] = useState('*/*')
  const [skills, setSkills] = useState<ChatSkill[]>(() => composerSkills(session))
  const [open, setOpen] = useState(false)
  const [tall, setTall] = useState(false)
  const [delivery, setDelivery] = useState<ChatDelivery>('queue')
  const composingRef = useRef(false)

  const expand = () => {
    setOpen(true)
    requestAnimationFrame(() => innerRef.current?.focus())
  }

  const collapse = () => {
    setMenuOpen(false)
    setMention(null)
    setOpen(false)
    setTall(false)
  }
  const longInput = messageNeedsFold(value)

  useEffect(() => {
    if (value.trim() && !messageNeedsFold(value)) setOpen(true)
  }, [value])

  useEffect(() => {
    setSkills(composerSkills(session))
  }, [session])

  useEffect(() => {
    let alive = true
    fetchChatSkills()
      .then((rows) => {
        if (!alive || rows.length === 0) return
        const builtins = composerSkills(session)
        const extra = rows.filter((row) => !builtins.some((item) => item.id === row.id))
        setSkills([...builtins, ...extra])
      })
      .catch(() => {
        /* 扫不到就用内置 herdr/mail */
      })
    return () => {
      alive = false
    }
  }, [session])

  const candidates = useMemo(() => {
    if (!mention) return []
    const q = mention.query.toLowerCase()
    const sorted = [...members].sort((a, b) => Number(b.isLeader) - Number(a.isLeader))
    if (!q) return sorted
    return sorted.filter(
      (m) =>
        `${m.name} ${m.kind} ${m.kind}-main`.toLowerCase().includes(q) ||
        (m.isLeader && 'leader'.startsWith(q)),
    )
  }, [mention, members])
  const showBroadcast = useMemo(() => {
    if (!mention) return false
    const q = mention.query.toLowerCase()
    return ['all', '所有人', 'everyone'].some((token) => token.startsWith(q))
  }, [mention])

  const targets = useMemo(() => parseMentionTargets(value, members), [value, members])

  const pick = (m: ChatMember) => {
    const el = innerRef.current
    const caret = el ? el.selectionStart : value.length
    if (!mention) return
    const next = `${value.slice(0, mention.start)}@${m.name} ${value.slice(caret)}`
    onChange(next)
    setMention(null)
    requestAnimationFrame(() => {
      if (el) {
        const pos = mention.start + m.name.length + 2
        el.focus()
        el.setSelectionRange(pos, pos)
      }
    })
  }

  const pickAll = () => {
    const el = innerRef.current
    const caret = el?.selectionStart ?? value.length
    if (!mention) return
    const inserted = '@all '
    const next = `${value.slice(0, mention.start)}${inserted}${value.slice(caret)}`
    onChange(next)
    setMention(null)
    requestAnimationFrame(() => {
      if (el) {
        const pos = mention.start + inserted.length
        el.focus()
        el.setSelectionRange(pos, pos)
      }
    })
  }

  const insertText = (text: string) => {
    const el = innerRef.current
    const start = el?.selectionStart ?? value.length
    const end = el?.selectionEnd ?? start
    const next = `${value.slice(0, start)}${text}${value.slice(end)}`
    onChange(next)
    setOpen(true)
    requestAnimationFrame(() => {
      if (!el) return
      const pos = start + text.length
      el.focus()
      el.setSelectionRange(pos, pos)
    })
  }

  const onValueChange = (v: string) => {
    onChange(v)
    const el = innerRef.current
    const caret = el ? el.selectionStart : v.length
    const m = mentionQueryAt(v, caret)
    setMention(m)
    setActiveIdx(0)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const optionCount = candidates.length + Number(showBroadcast)
    if (mention && optionCount > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActiveIdx((i) => (i + 1) % optionCount)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActiveIdx((i) => (i - 1 + optionCount) % optionCount)
        return
      }
      if ((e.key === 'Enter' && shouldSendOnEnter({
        key: e.key,
        shiftKey: false,
        isComposing: e.nativeEvent.isComposing || composingRef.current,
        keyCode: e.keyCode,
      })) || e.key === 'Tab') {
        e.preventDefault()
        if (showBroadcast && activeIdx === 0) pickAll()
        else pick(candidates[activeIdx - Number(showBroadcast)])
        return
      }
      if (e.key === 'Escape') {
        setMention(null)
        return
      }
    }
    if (e.key === 'Escape' && menuOpen) {
      setMenuOpen(false)
      return
    }
    if (
      shouldSendOnEnter({
        key: e.key,
        shiftKey: e.shiftKey,
        isComposing: e.nativeEvent.isComposing || composingRef.current,
        keyCode: e.keyCode,
      })
    ) {
      e.preventDefault()
      onSend(delivery)
    }
  }

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const image = clipboardImageFile(Array.from(e.clipboardData.items))
    if (!image || disabled || attaching) return
    e.preventDefault()
    onAttach(image)
  }

  useEffect(() => {
    if (!menuOpen) return
    const onDoc = (event: MouseEvent) => {
      const node = menuRef.current
      if (node && event.target instanceof Node && !node.contains(event.target)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [menuOpen])

  const pickFile = (accept: string) => {
    setFileAccept(accept)
    setMenuOpen(false)
    requestAnimationFrame(() => fileRef.current?.click())
  }

  return (
    <div className={`gc-composer-wrap${open ? '' : ' is-collapsed'}`}>
      <div className={`gc-composer${open ? '' : ' is-collapsed'}`}>
        {mention && (showBroadcast || candidates.length > 0) && (
          <div className="gc-mention" role="listbox">
            {showBroadcast && (
              <button
                type="button"
                role="option"
                aria-selected={activeIdx === 0}
                className={`gc-mention-item${activeIdx === 0 ? ' is-active' : ''}`}
                onMouseDown={(e) => {
                  e.preventDefault()
                  pickAll()
                }}
              >
                <span aria-hidden>📢</span>
                <span>所有人</span>
                <span className="gc-mention-kind">@all 广播</span>
              </button>
            )}
            {candidates.map((m, i) => {
              const optionIdx = i + Number(showBroadcast)
              return (
                <button
                  key={m.paneId}
                  type="button"
                  role="option"
                  aria-selected={optionIdx === activeIdx}
                  className={`gc-mention-item${optionIdx === activeIdx ? ' is-active' : ''}`}
                  onMouseDown={(e) => {
                    e.preventDefault()
                    pick(m)
                  }}
                >
                  <span aria-hidden>{agentEmoji(m.kind)}</span>
                  <span>{m.name}</span>
                  {m.isLeader && <span className="gc-leader-badge">Leader</span>}
                  <span className="gc-mention-kind">{m.kind}</span>
                </button>
              )
            })}
          </div>
        )}
        <textarea
          id="gc-composer-input"
          ref={textareaRef}
          rows={open ? (tall ? 12 : 3) : 1}
          value={value}
          disabled={disabled}
          aria-hidden={!open}
          tabIndex={open ? 0 : -1}
          placeholder={disabled ? '先选择或创建一个会话…' : '@leader 分派任务；+ 添加附件 / Skill；可粘贴截图'}
          onFocus={() => setOpen(true)}
          onChange={(e) => onValueChange(e.target.value)}
          onPaste={onPaste}
          onCompositionStart={() => { composingRef.current = true }}
          onCompositionEnd={() => {
            window.setTimeout(() => { composingRef.current = false }, 0)
          }}
          onKeyDown={onKeyDown}
        />
        <div className="gc-composer-bar">
          <div className="gc-attach" ref={menuRef}>
            <button
              type="button"
              className="gc-composer-add"
              title="添加附件或 Skill"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((open) => !open)}
              disabled={disabled || attaching}
            >
              ＋
            </button>
            {menuOpen && (
              <div className="gc-attach-menu" role="menu">
                <div className="gc-attach-primary">
                  <button type="button" role="menuitem" onClick={() => pickFile('*/*')}>
                    上传文件
                  </button>
                  <button type="button" role="menuitem" onClick={() => pickFile('image/*')}>
                    上传图片
                  </button>
                  <div className="gc-attach-hint">也可 Ctrl+V / ⌘V 粘贴截图</div>
                </div>
                <div className="gc-attach-sep">Skill</div>
                <div className="gc-attach-skills" role="group" aria-label="Skills">
                  {skills.map((skill) => (
                    <button
                      key={skill.id}
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        insertText(skill.insert)
                        setMenuOpen(false)
                      }}
                    >
                      {skill.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept={fileAccept}
            hidden
            onChange={(e) => {
              const file = e.target.files?.[0]
              e.target.value = ''
              if (file) onAttach(file)
            }}
          />
          {!open && (
            <button
              type="button"
              className={`gc-composer-open${longInput ? ' is-long' : ''}`}
              data-testid="gc-composer-preview"
              disabled={disabled}
              onClick={() => expand()}
            >
              {disabled ? '先选择或创建一个会话…' : composerPreviewLabel(value)}
            </button>
          )}
          {open && (
            <span className="gc-composer-target">
              {attaching ? (
                '正在上传附件…'
              ) : targets.length > 0 ? (
                <>
                  发送给 <b>{targets.map((t) => t.name).join('、')}</b>
                </>
              ) : leader ? (
                <>
                  发送给 <b>{leader.name}</b>（默认 Leader）
                </>
              ) : (
                '暂无成员'
              )}
            </span>
          )}
          {open && (
            <div className="gc-delivery" role="radiogroup" aria-label="消息类型">
              <button
                type="button"
                role="radio"
                aria-checked={delivery === 'queue'}
                className={`gc-delivery-opt${delivery === 'queue' ? ' is-active' : ''}`}
                title="等对方空闲再处理（默认）"
                onClick={() => setDelivery('queue')}
              >
                排队
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={delivery === 'interrupt'}
                className={`gc-delivery-opt${delivery === 'interrupt' ? ' is-active' : ''}`}
                title="立刻打断正在做的事"
                onClick={() => setDelivery('interrupt')}
              >
                打断
              </button>
            </div>
          )}
          {open && value.trim() !== '' && (
            <button
              type="button"
              className="gc-composer-toggle gc-composer-toggle--grow"
              aria-pressed={tall}
              aria-label={tall ? '收起输入全文' : '展开输入全文'}
              title={tall ? '收起输入全文' : '展开输入全文'}
              onClick={() => setTall((current) => !current)}
            >
              {tall ? '收起全文' : '展开全文'}
            </button>
          )}
          <button
            type="button"
            className="gc-composer-toggle"
            aria-expanded={open}
            aria-controls="gc-composer-input"
            aria-label={open ? '收起输入框' : '展开输入框'}
            title={open ? '收起输入框' : '展开输入框'}
            onClick={() => {
              if (open) collapse()
              else expand()
            }}
          >
            {open ? '收起' : '展开'}
          </button>
          <button
            type="button"
            className="gc-send"
            title={delivery === 'queue' ? '排队发送（Enter）' : '立刻打断发送（Enter）'}
            disabled={disabled || attaching || value.trim() === ''}
            onClick={() => onSend(delivery)}
          >
            <IconSendOutline16 />
          </button>
        </div>
      </div>
    </div>
  )
}
