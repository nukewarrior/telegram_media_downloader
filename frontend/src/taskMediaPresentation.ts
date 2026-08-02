export type TaskMediaThumbnailInput = {
  media_type: string
  status: string
  thumbnail_url: string | null | undefined
}

export type TaskMediaPreviewInput = TaskMediaThumbnailInput & {
  content_url: string | null | undefined
  download_url: string | null | undefined
}

export type TaskMediaPreview = {
  content_url: string
  download_url: string | null
}

export type TaskMediaFallback = 'image' | 'video' | 'audio' | 'document'

export function taskMediaThumbnail(item: TaskMediaThumbnailInput, failed = false): string | null {
  if (failed || item.status !== 'COMPLETED' || !item.thumbnail_url) return null
  return item.thumbnail_url
}

export function taskMediaPreview(item: TaskMediaPreviewInput, failed = false): TaskMediaPreview | null {
  if (failed || item.status !== 'COMPLETED' || !['PHOTO', 'VIDEO'].includes(item.media_type) || !item.thumbnail_url || !item.content_url) return null
  return { content_url: item.content_url, download_url: item.download_url ?? null }
}

export function taskMediaFallback(mediaType: string): TaskMediaFallback {
  if (mediaType === 'PHOTO') return 'image'
  if (mediaType === 'VIDEO') return 'video'
  if (mediaType === 'AUDIO') return 'audio'
  return 'document'
}
