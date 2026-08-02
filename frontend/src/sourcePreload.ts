export interface SourcePreloadState {
  nextCursor: string | null
  loading: boolean
  loadingMore: boolean
}

export function shouldPreloadSourceMedia(state: SourcePreloadState) {
  return Boolean(state.nextCursor) && !state.loading && !state.loadingMore
}

export function isCurrentSourceRequest(requestEpoch: number, currentEpoch: number) {
  return requestEpoch === currentEpoch
}

export function retainSourceChatSelection(selectedChatId: string, availableChatIds: readonly string[]) {
  return selectedChatId && availableChatIds.includes(selectedChatId) ? selectedChatId : ''
}
