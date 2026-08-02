import assert from 'node:assert/strict'
import test from 'node:test'
import { taskMediaFallback, taskMediaThumbnail } from '../src/taskMediaPresentation.ts'

test('uses the shared archive thumbnail for completed photos and videos', () => {
  assert.equal(taskMediaThumbnail({ media_type: 'PHOTO', status: 'COMPLETED', thumbnail_url: '/api/archives/media/1/thumbnail?v=hash' }), '/api/archives/media/1/thumbnail?v=hash')
  assert.equal(taskMediaThumbnail({ media_type: 'VIDEO', status: 'COMPLETED', thumbnail_url: '/api/archives/media/2/thumbnail?v=hash' }), '/api/archives/media/2/thumbnail?v=hash')
})

test('falls back when a thumbnail fails or the file is not complete', () => {
  const item = { media_type: 'PHOTO', status: 'COMPLETED', thumbnail_url: '/thumbnail' }
  assert.equal(taskMediaThumbnail(item, true), null)
  assert.equal(taskMediaThumbnail({ ...item, status: 'DOWNLOADING' }), null)
  assert.equal(taskMediaThumbnail({ ...item, thumbnail_url: null }), null)
})

test('chooses stable type fallbacks for ordinary files and audio', () => {
  assert.equal(taskMediaFallback('DOCUMENT'), 'document')
  assert.equal(taskMediaFallback('AUDIO'), 'audio')
  assert.equal(taskMediaFallback('UNKNOWN'), 'document')
})
