import assert from 'node:assert/strict'
import test from 'node:test'
import { isCurrentSourceRequest, retainSourceChatSelection, shouldPreloadSourceMedia } from '../src/sourcePreload.ts'

test('only preloads when another cursor is available and no request is active', () => {
  assert.equal(shouldPreloadSourceMedia({ nextCursor: null, loading: false, loadingMore: false }), false)
  assert.equal(shouldPreloadSourceMedia({ nextCursor: 'cursor', loading: true, loadingMore: false }), false)
  assert.equal(shouldPreloadSourceMedia({ nextCursor: 'cursor', loading: false, loadingMore: true }), false)
  assert.equal(shouldPreloadSourceMedia({ nextCursor: 'cursor', loading: false, loadingMore: false }), true)
})

test('recognizes responses from an outdated source request', () => {
  assert.equal(isCurrentSourceRequest(4, 4), true)
  assert.equal(isCurrentSourceRequest(3, 4), false)
})

test('does not select the first source chat automatically', () => {
  assert.equal(retainSourceChatSelection('', ['first', 'second']), '')
})

test('retains an available source chat and clears a missing one', () => {
  assert.equal(retainSourceChatSelection('second', ['first', 'second']), 'second')
  assert.equal(retainSourceChatSelection('missing', ['first', 'second']), '')
})
