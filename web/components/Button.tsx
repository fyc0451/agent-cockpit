import { useId, type ButtonHTMLAttributes } from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost' | 'icon'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
}

/**
 * disabled 不用原生属性：aria-disabled="true" + 保持可聚焦（键盘/读屏可达），
 * title 给出原因，并用 aria-describedby 关联一个 sr-only reason 节点（不只靠 title），
 * 拦截 click/Enter/Space 激活。样式通过 [aria-disabled] 维持 disabled 视觉。
 */
export function Button({
  variant = 'secondary',
  className,
  type,
  disabled,
  onClick,
  onKeyDown,
  title,
  ...rest
}: ButtonProps) {
  const descId = useId()
  const cls = ['btn', `btn--${variant}`, className].filter(Boolean).join(' ')
  if (disabled) {
    const swallow = (e: { preventDefault: () => void; stopPropagation: () => void }) => {
      e.preventDefault()
      e.stopPropagation()
    }
    return (
      <>
        <button
          type={type ?? 'button'}
          className={cls}
          aria-disabled="true"
          aria-describedby={title ? descId : undefined}
          title={title}
          onClick={swallow}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') swallow(e)
            onKeyDown?.(e)
          }}
          {...rest}
        />
        {title ? (
          <span id={descId} className="sr-only">
            {title}
          </span>
        ) : null}
      </>
    )
  }
  return (
    <button
      type={type ?? 'button'}
      className={cls}
      title={title}
      onClick={onClick}
      onKeyDown={onKeyDown}
      {...rest}
    />
  )
}
