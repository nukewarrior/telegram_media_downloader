// Production UI is served by FastAPI, so API requests must stay on the same host
// that the user opened in their browser (including LAN deployments).
const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'
export const apiResourceUrl = (path: string | null) => path ? `${API_BASE}${path.replace(/^\/api/, '')}` : null

export type ConnectionStatus = 'unconfigured' | 'disconnected' | 'connected' | 'invalid'
export type AppState = { configured: boolean; apiConfigured: boolean; archiveTimezone: string | null; accountConnected: boolean; accountName: string | null; connectionStatus: ConnectionStatus; downloadRoot: string; demoMode: boolean }
export type LoginResult = AppState & { passwordRequired: boolean; attemptId?: string; accountPhone?: string }
export type DownloadRuntime = { maxConcurrency: number; effectiveConcurrency: number; activeDownloads: number; waitUntil: string | null }
export type Settings = { apiId: string | null; apiHashConfigured: boolean; archiveTimezone: string | null; accountConnected: boolean; accountName: string | null; accountPhone: string | null; connectionStatus: ConnectionStatus; downloadRoot: string; trustedLanWarning: string; download: DownloadRuntime }
export type TaskMedia = { id: number; task_id: number; message_id: number; filename: string; media_type: string; mime_type: string | null; size_bytes: number; message_date: string; status: string; error_message: string | null; downloaded_bytes: number; speed_bytes_per_second: number; updated_at: string; revision: number; percent: number; attempt_count: number; next_retry_at: string | null; failure_category: string | null }
export type Task = { id: number; chat_id: string; chat_title: string; chat_handle: string | null; status: string; total_count: number; completed_count: number; failed_count: number; total_bytes: number; downloaded_bytes: number; current_file: string | null; speed_bytes_per_second: number; media_revision: number; error_message: string | null; download_wait_until: string | null; filters: Record<string, unknown>; created_at: string; updated_at: string; activeMedia?: TaskMedia[]; downloadRuntime?: DownloadRuntime }
export type TaskMediaPage = { items: TaskMedia[]; page: number; pageSize: number; total: number; mediaRevision: number }
export type Chat = { id: string; title: string; handle: string | null; type: string }
export type SourcePreview = { id: number; status: 'PENDING' | 'DOWNLOADING' | 'READY' | 'FAILED' | 'CONSUMED'; downloaded_bytes: number; size_bytes: number; error_message: string | null; content_url: string | null }
export type SourceMedia = { message_id: number; filename: string; media_type: string; mime_type: string | null; size_bytes: number; message_date: string; archived: boolean; archive_id: number | null; queued: boolean; thumbnail_status: string; thumbnail_url: string | null; preview: SourcePreview | null }
export type SourceMediaPage = { items: SourceMedia[]; next_cursor: string | null }
export type ArchiveItem = { id: number; chat_id: string; chat_title: string; message_id: number; filename: string; media_type: string; mime_type: string | null; size_bytes: number; message_date: string; canonical_path: string; thumbnail_path: string | null; thumbnail_status: string; thumbnail_error: string | null; thumbnail_url: string | null; content_url: string | null; download_url: string }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? '请求失败，请稍后重试')
  }
  return response.json() as Promise<T>
}

export const api = {
  state: () => request<AppState>('/app-state'),
  setup: (api_id: string, api_hash: string) => request<AppState>('/setup', { method: 'PUT', body: JSON.stringify({ api_id, api_hash }) }),
  timezones: () => request<{ timezones: string[] }>('/timezones'),
  updateArchiveTimezone: (archive_timezone: string) => request<AppState>('/settings/archive-timezone', { method: 'PUT', body: JSON.stringify({ archive_timezone }) }),
  settings: () => request<Settings>('/settings'),
  updateApi: (api_id: string, api_hash: string) => request<AppState>('/settings/api', { method: 'PUT', body: JSON.stringify({ api_id, api_hash }) }),
  updateDownloadConcurrency: (maxConcurrency: number) => request<DownloadRuntime>('/settings/download-concurrency', { method: 'PUT', body: JSON.stringify({ max_concurrency: maxConcurrency }) }),
  sendCode: (phone: string) => request<{ attemptId: string; passwordRequired: boolean; demoHint: string | null }>('/telegram/login/send-code', { method: 'POST', body: JSON.stringify({ phone }) }),
  verifyCode: (attempt_id: string, code: string) => request<LoginResult>('/telegram/login/verify-code', { method: 'POST', body: JSON.stringify({ attempt_id, code }) }),
  verifyPassword: (attempt_id: string, password: string) => request<LoginResult>('/telegram/login/verify-password', { method: 'POST', body: JSON.stringify({ attempt_id, password }) }),
  logout: () => request<AppState>('/telegram/logout', { method: 'POST' }),
  chats: () => request<Chat[]>('/chats'),
  sourceMedia: (chatId: string, query: Record<string, string> = {}) => request<SourceMediaPage>(`/sources/${encodeURIComponent(chatId)}/media?${new URLSearchParams(query)}`),
  preview: (chatId: string, item: SourceMedia) => request<SourcePreview>(`/sources/${encodeURIComponent(chatId)}/media/${item.message_id}/preview`, { method: 'POST', body: JSON.stringify({ filename: item.filename, media_type: item.media_type, mime_type: item.mime_type, size_bytes: item.size_bytes, message_date: item.message_date }) }),
  previewStatus: (id: number) => request<SourcePreview>(`/previews/${id}`),
  stopPreview: (id: number) => request<SourcePreview>(`/previews/${id}`, { method: 'DELETE' }),
  createSelectionTask: (payload: { chat_id: string; chat_title: string; chat_handle: string | null; message_ids: number[] }) => request<Task>('/tasks/selection', { method: 'POST', body: JSON.stringify(payload) }),
  tasks: () => request<Task[]>('/tasks'),
  task: (id: number) => request<Task>(`/tasks/${id}`),
  taskMedia: (id: number, page = 1) => request<TaskMediaPage>(`/tasks/${id}/media?page=${page}&page_size=50`),
  scan: (payload: unknown) => request<{ totalCount: number; totalBytes: number; duplicateCount: number }>('/tasks/scan', { method: 'POST', body: JSON.stringify(payload) }),
  createTask: (payload: unknown) => request<Task>('/tasks', { method: 'POST', body: JSON.stringify(payload) }),
  action: (id: number, action: 'pause' | 'resume' | 'cancel' | 'retry') => request<Task>(`/tasks/${id}/${action}`, { method: 'POST' }),
  archiveChats: () => request<{ id: string; title: string; item_count: number }[]>('/archives/chats'),
  archiveMedia: (query: Record<string, string> = {}) => request<ArchiveItem[]>(`/archives/media?${new URLSearchParams(query)}`),
  archiveDetail: (id: number) => request<ArchiveItem>(`/archives/media/${id}`),
  eventsUrl: (id: number, afterRevision = 0) => `${API_BASE}/tasks/${id}/events?after_revision=${afterRevision}`,
}
