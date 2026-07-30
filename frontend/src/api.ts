// Production UI is served by FastAPI, so API requests must stay on the same host
// that the user opened in their browser (including LAN deployments).
const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

export type ConnectionStatus = 'unconfigured' | 'disconnected' | 'connected' | 'invalid'
export type AppState = { configured: boolean; accountConnected: boolean; accountName: string | null; connectionStatus: ConnectionStatus; downloadRoot: string; demoMode: boolean }
export type LoginResult = AppState & { passwordRequired: boolean; attemptId?: string; accountPhone?: string }
export type Task = { id: number; chat_id: string; chat_title: string; chat_handle: string | null; status: string; total_count: number; completed_count: number; failed_count: number; total_bytes: number; downloaded_bytes: number; current_file: string | null; speed_bytes_per_second: number; error_message: string | null; filters: Record<string, unknown>; created_at: string; updated_at: string }
export type Chat = { id: string; title: string; handle: string | null; type: string }
export type ArchiveItem = { id: number; chat_id: string; chat_title: string; message_id: number; filename: string; media_type: string; mime_type: string | null; size_bytes: number; message_date: string; canonical_path: string; thumbnail_path: string | null; thumbnail_status: string }

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
  settings: () => request<Record<string, unknown>>('/settings'),
  updateApi: (api_id: string, api_hash: string) => request<AppState>('/settings/api', { method: 'PUT', body: JSON.stringify({ api_id, api_hash }) }),
  sendCode: (phone: string) => request<{ attemptId: string; passwordRequired: boolean; demoHint: string | null }>('/telegram/login/send-code', { method: 'POST', body: JSON.stringify({ phone }) }),
  verifyCode: (attempt_id: string, code: string) => request<LoginResult>('/telegram/login/verify-code', { method: 'POST', body: JSON.stringify({ attempt_id, code }) }),
  verifyPassword: (attempt_id: string, password: string) => request<LoginResult>('/telegram/login/verify-password', { method: 'POST', body: JSON.stringify({ attempt_id, password }) }),
  logout: () => request<AppState>('/telegram/logout', { method: 'POST' }),
  chats: () => request<Chat[]>('/chats'),
  tasks: () => request<Task[]>('/tasks'),
  task: (id: number) => request<Task>(`/tasks/${id}`),
  scan: (payload: unknown) => request<{ totalCount: number; totalBytes: number; duplicateCount: number }>('/tasks/scan', { method: 'POST', body: JSON.stringify(payload) }),
  createTask: (payload: unknown) => request<Task>('/tasks', { method: 'POST', body: JSON.stringify(payload) }),
  action: (id: number, action: 'pause' | 'resume' | 'cancel' | 'retry') => request<Task>(`/tasks/${id}/${action}`, { method: 'POST' }),
  archiveChats: () => request<{ id: string; title: string; item_count: number }[]>('/archives/chats'),
  archiveMedia: (query: Record<string, string> = {}) => request<ArchiveItem[]>(`/archives/media?${new URLSearchParams(query)}`),
  archiveDetail: (id: number) => request<ArchiveItem>(`/archives/media/${id}`),
  eventsUrl: (id: number) => `${API_BASE}/tasks/${id}/events`,
}
