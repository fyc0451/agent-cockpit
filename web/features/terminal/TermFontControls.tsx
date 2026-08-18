import { TERM_FONT_MAX, TERM_FONT_MIN, stepTermFontSize } from './termFont'

interface TermFontControlsProps {
  value: number
  onChange: (size: number) => void
}

export function TermFontControls({ value, onChange }: TermFontControlsProps) {
  return (
    <div className="term-font-controls" role="group" aria-label="终端字体大小">
      <button
        type="button"
        className="term-font-button"
        aria-label="缩小字体"
        title="缩小字体"
        disabled={value <= TERM_FONT_MIN}
        onClick={() => onChange(stepTermFontSize(value, -1))}
      >
        A−
      </button>
      <span className="term-font-size" data-testid="term-font-size">
        {value}
      </span>
      <button
        type="button"
        className="term-font-button"
        aria-label="放大字体"
        title="放大字体"
        disabled={value >= TERM_FONT_MAX}
        onClick={() => onChange(stepTermFontSize(value, 1))}
      >
        A+
      </button>
    </div>
  )
}
