import assert from 'node:assert/strict'
import test from 'node:test'
import { clearChatCache, readChatCache, writeChatCache } from '../src/chatCache.ts'

const values = new Map()
globalThis.window = {
  localStorage: {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  },
}

test('persists chat snapshots and marks expired snapshots stale', () => {
  writeChatCache({ chats: [{ id: '1', title: '聊天', handle: null, type: 'GROUP' }], refreshedAt: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString(), isStale: false, lastRefreshError: null })
  const snapshot = readChatCache()
  assert.equal(snapshot?.chats[0].title, '聊天')
  assert.equal(snapshot?.isStale, true)
})

test('clears the persisted chat snapshot', () => {
  clearChatCache()
  assert.equal(readChatCache(), null)
})
