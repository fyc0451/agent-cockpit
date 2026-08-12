import type { ButtonHTMLAttributes } from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost' | 'icon'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
}

/**
 * disabled 不用原生属性：aria-disabled="true" + 保持可聚焦（键盘/读屏可达），
 * title 给出原因（作为 accessible description），并拦截 click/Enter/Space 激活。
 * 样式通过 [aria-disabled] 维持 disabled 视觉。
 */
export function Button({
  variant = 'secondary',
  className,
  type,
  disabled,
  onClick,
  onKeyDown,
  ...rest
}: ButtonProps) {
  const cls = ['btn', `btn--${variant}`, className].filter(Boolean).join(' ')
  if (disabled) {
    const swallow = (e: { preventDefault: () => void; stopPropagation: () => void }) => {
      e.preventDefault()
      e.stopPropagation()
    }
    return (
      <button
        type={type ?? 'button'}
        className={cls}
        aria-disabled="true"
        onClick={swallow}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') swallow(e)
          onKeyDown?.(e)
        }}
        {...rest}
      />
    )
  }
  return (
    <button type={type ?? 'button'} className={cls} onClick={onClick} onKeyDown={onKeyDown} {...rest} />
  )
}
