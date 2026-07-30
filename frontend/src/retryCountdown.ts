export function retryCountdown(status: string, nextRetryAt: string | null, currentTimeMs = Date.now()): string | null {
  if (status !== 'RETRY_WAIT' || !nextRetryAt) return null

  const retryAtMs = Date.parse(nextRetryAt)
  if (Number.isNaN(retryAtMs)) return null

  const remainingSeconds = Math.ceil((retryAtMs - currentTimeMs) / 1000)
  return remainingSeconds > 0 ? `将在 ${remainingSeconds} 秒后自动重试` : '正在安排重试…'
}
