import type { ChatListSnapshot } from './api'

const STORAGE_KEY = 'telegram-media-archiver.chat-list.v1'
const STALE_AFTER_MS = 24 * 60 * 60 * 1000

function storage(): Storage | null {
  return typeof window === 'undefined' ? null : window.localStorage
}

function isSnapshot(value: unknown): value is ChatListSnapshot {
  if (!value || typeof value !== 'object') return false
  const snapshot = value as Partial<ChatListSnapshot>
  return Array.isArray(snapshot.chats) && typeof snapshot.isStale === 'boolean'
}

export function readChatCache(): ChatListSnapshot | null {
  try {
    const value = storage()?.getItem(STORAGE_KEY)
    if (!value) return null
    const snapshot: unknown = JSON.parse(value)
    if (!isSnapshot(snapshot)) return null
    const refreshedAt = snapshot.refreshedAt ? Date.parse(snapshot.refreshedAt) : Number.NaN
    return { ...snapshot, isStale: snapshot.isStale || !Number.isFinite(refreshedAt) || Date.now() - refreshedAt >= STALE_AFTER_MS }
  } catch {
    return null
  }
}

export function writeChatCache(snapshot: ChatListSnapshot): void {
  try { storage()?.setItem(STORAGE_KEY, JSON.stringify(snapshot)) } catch { /* Storage may be disabled or full. */ }
}

export function clearChatCache(): void {
  try { storage()?.removeItem(STORAGE_KEY) } catch { /* Storage may be disabled. */ }
}
