export type TaskMediaThumbnailInput = {
  media_type: string
  status: string
  thumbnail_url: string | null | undefined
}

export type TaskMediaFallback = 'image' | 'video' | 'audio' | 'document'

export function taskMediaThumbnail(item: TaskMediaThumbnailInput, failed = false): string | null {
  if (failed || item.status !== 'COMPLETED' || !item.thumbnail_url) return null
  return item.thumbnail_url
}

export function taskMediaFallback(mediaType: string): TaskMediaFallback {
  if (mediaType === 'PHOTO') return 'image'
  if (mediaType === 'VIDEO') return 'video'
  if (mediaType === 'AUDIO') return 'audio'
  return 'document'
}
