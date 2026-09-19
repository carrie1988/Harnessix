/** 从 Coding Agent 工具边界提炼的离线评测函数。 */

export function normalizePath(input) {
  return input
}

export function retryDelay(attempt, baseMs = 100) {
  if (attempt === 0) return baseMs
  if (attempt === 1) return baseMs * 2
  return baseMs * 2 ** attempt
}

export function sanitizeTerminalUrl(value) {
  return value
}
