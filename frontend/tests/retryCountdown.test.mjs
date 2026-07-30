import assert from 'node:assert/strict'
import test from 'node:test'
import { retryCountdown } from '../src/retryCountdown.ts'

const now = Date.parse('2026-07-30T10:00:00.000Z')

test('shows the remaining retry seconds rounded up', () => {
  assert.equal(retryCountdown('RETRY_WAIT', '2026-07-30T10:00:05.100Z', now), '将在 6 秒后自动重试')
})

test('shows scheduling state after the retry time has elapsed', () => {
  assert.equal(retryCountdown('RETRY_WAIT', '2026-07-30T09:59:59.000Z', now), '正在安排重试…')
})

test('ignores missing or invalid retry timestamps', () => {
  assert.equal(retryCountdown('RETRY_WAIT', null, now), null)
  assert.equal(retryCountdown('RETRY_WAIT', 'not-a-date', now), null)
})

test('does not render a countdown for other media states', () => {
  assert.equal(retryCountdown('DOWNLOADING', '2026-07-30T10:00:05.000Z', now), null)
})
