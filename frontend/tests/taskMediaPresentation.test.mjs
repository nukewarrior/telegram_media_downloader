import assert from 'node:assert/strict'
import test from 'node:test'
import { taskMediaFallback, taskMediaPreview, taskMediaThumbnail } from '../src/taskMediaPresentation.ts'

test('uses the shared archive thumbnail for completed photos and videos', () => {
  assert.equal(taskMediaThumbnail({ media_type: 'PHOTO', status: 'COMPLETED', thumbnail_url: '/api/archives/media/1/thumbnail?v=hash' }), '/api/archives/media/1/thumbnail?v=hash')
  assert.equal(taskMediaThumbnail({ media_type: 'VIDEO', status: 'COMPLETED', thumbnail_url: '/api/archives/media/2/thumbnail?v=hash' }), '/api/archives/media/2/thumbnail?v=hash')
})

test('allows completed photos and videos with ready thumbnails to open their archive resources', () => {
  const photo = taskMediaPreview({
    media_type: 'PHOTO',
    status: 'COMPLETED',
    thumbnail_url: '/api/archives/media/1/thumbnail?v=hash',
    content_url: '/api/archives/media/1/content?v=hash',
    download_url: '/api/archives/media/1/download?v=hash',
  })
  const video = taskMediaPreview({
    media_type: 'VIDEO',
    status: 'COMPLETED',
    thumbnail_url: '/api/archives/media/2/thumbnail?v=hash',
    content_url: '/api/archives/media/2/content?v=hash',
    download_url: null,
  })
  assert.deepEqual(photo, { content_url: '/api/archives/media/1/content?v=hash', download_url: '/api/archives/media/1/download?v=hash' })
  assert.deepEqual(video, { content_url: '/api/archives/media/2/content?v=hash', download_url: null })
})

test('falls back when a thumbnail fails or the file is not complete', () => {
  const item = { media_type: 'PHOTO', status: 'COMPLETED', thumbnail_url: '/thumbnail' }
  assert.equal(taskMediaThumbnail(item, true), null)
  assert.equal(taskMediaThumbnail({ ...item, status: 'DOWNLOADING' }), null)
  assert.equal(taskMediaThumbnail({ ...item, thumbnail_url: null }), null)
  assert.equal(taskMediaPreview({ ...item, content_url: '/content', download_url: '/download' }, true), null)
  assert.equal(taskMediaPreview({ ...item, status: 'DOWNLOADING', content_url: '/content', download_url: '/download' }), null)
  assert.equal(taskMediaPreview({ ...item, thumbnail_url: null, content_url: '/content', download_url: '/download' }), null)
  assert.equal(taskMediaPreview({ ...item, content_url: null, download_url: '/download' }), null)
  assert.equal(taskMediaPreview({ ...item, media_type: 'DOCUMENT', content_url: '/content', download_url: '/download' }), null)
})

test('chooses stable type fallbacks for ordinary files and audio', () => {
  assert.equal(taskMediaFallback('DOCUMENT'), 'document')
  assert.equal(taskMediaFallback('AUDIO'), 'audio')
  assert.equal(taskMediaFallback('UNKNOWN'), 'document')
})
