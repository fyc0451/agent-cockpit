interface AgentIconProps {
  kind: string
  size?: number
}

/** 群成员头像用的 Agent 专属矢量标记；不依赖外部图片资源。 */
export function AgentIcon({ kind, size = 17 }: AgentIconProps) {
  const normalized = kind.toLowerCase()
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    'aria-hidden': true,
    'data-agent-icon': normalized,
  }

  if (normalized === 'codex') {
    return (
      <svg {...common}>
        <path d="M12 2.7 15 7l5.1.9-3.5 3.8.5 5.1-4.1-2.2-4.1 2.2.5-5.1L5.9 7.9 11 7l1-4.3Z" fill="currentColor" />
        <circle cx="12" cy="11.5" r="2.1" fill="var(--gc-agent-mark-cutout, #fff)" />
      </svg>
    )
  }
  if (normalized === 'claude') {
    return (
      <svg {...common}>
        <path d="M17.8 5.4a7.3 7.3 0 1 0 0 13.2l-1.2-3a4.1 4.1 0 1 1 0-7.2l1.2-3Z" fill="currentColor" />
        <path d="M17.1 8.5h2.4v7h-2.4z" fill="currentColor" />
      </svg>
    )
  }
  if (normalized === 'kimi') {
    return (
      <svg {...common}>
        <path d="M18.5 4.2a8.5 8.5 0 1 0 1.3 12.3A7.1 7.1 0 1 1 18.5 4.2Z" fill="currentColor" />
        <path d="m17.2 4 1 .8 1.2-.3-.5 1.1.5 1.1-1.2-.3-1 .8.2-1.3L16.6 4.7l1.2.3.4-1Z" fill="currentColor" />
      </svg>
    )
  }
  if (normalized === 'opencode') {
    return (
      <svg {...common}>
        <path d="m9.1 6-5.3 6 5.3 6M14.9 6l5.3 6-5.3 6M13.5 3.8l-3 16.4" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    )
  }
  if (normalized === 'grok') {
    return (
      <svg {...common}>
        <path d="m5 4 14 16M19 4 5 20" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
        <path d="m13.2 2.5-4 8h4l-2.4 11 6-9h-4.1l.5-10Z" fill="currentColor" />
      </svg>
    )
  }
  if (normalized === 'qoder' || normalized === 'qodercli' || normalized === 'qodercn') {
    return (
      <svg {...common}>
        <path d="M4.5 7.2 12 3l7.5 4.2v9.6L12 21l-7.5-4.2V7.2Z" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
        <path d="M12 3v18M4.5 7.2 12 11l7.5-3.8" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
      </svg>
    )
  }
  return (
    <svg {...common}>
      <rect x="4" y="5" width="16" height="14" rx="4" fill="currentColor" />
      <circle cx="9" cy="12" r="1.5" fill="var(--gc-agent-mark-cutout, #fff)" />
      <circle cx="15" cy="12" r="1.5" fill="var(--gc-agent-mark-cutout, #fff)" />
    </svg>
  )
}
