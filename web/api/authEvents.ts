type UnauthorizedListener = () => void

const unauthorizedListeners = new Set<UnauthorizedListener>()

export function subscribeUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

export function reportUnauthorized(): void {
  for (const listener of unauthorizedListeners) listener()
}

export function noteAuthFailure(error: unknown): void {
  if (!error || typeof error !== 'object') return
  const status = 'status' in error ? error.status : undefined
  const code = 'code' in error ? error.code : undefined
  const message = 'message' in error ? error.message : undefined
  if (status === 401 || code === 'unauthenticated' || message === '未认证') {
    reportUnauthorized()
  }
}
