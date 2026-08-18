/** 极简 className 组合（clsx 的布尔过滤子集）。 */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
