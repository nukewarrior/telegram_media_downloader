<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowLeft, CirclePause, Clock3, FileAudio, FileText, FileWarning, Gauge, Image, Play, RefreshCw, Trash2, Video, XCircle } from 'lucide-vue-next'
import DeleteConfirmDialog from '../components/DeleteConfirmDialog.vue'
import DeleteResultPanel from '../components/DeleteResultPanel.vue'
import { api, apiResourceUrl, type DeleteOperation, type Task, type TaskMedia, type TaskMediaPage } from '../api'
import { retryCountdown } from '../retryCountdown'
import { taskMediaFallback, taskMediaPreview, taskMediaThumbnail } from '../taskMediaPresentation'
import MediaViewer, { type MediaViewerItem } from '../components/MediaViewer.vue'

const route = useRoute()
const task = ref<Task | null>(null)
const mediaPage = ref<TaskMediaPage | null>(null)
const error = ref('')
const confirmCancel = ref(false)
const streamState = ref<'connected' | 'reconnecting'>('connected')
const retryClock = ref(Date.now())
const failedThumbnailIds = ref(new Set<number>())
const selectedMedia = ref<TaskMedia | null>(null)
const selectedMediaIds = ref(new Set<number>())
const confirmDeleteOpen = ref(false)
const deleting = ref(false)
const deleteOperation = ref<DeleteOperation | null>(null)
const deleteError = ref('')
let source: EventSource | null = null
let mediaSyncInFlight = false
let retryClockTimer: number | null = null
const taskId = Number(route.params.id)
const bytes = (value: number) => value >= 1_000_000_000 ? `${(value / 1_000_000_000).toFixed(1)} GB` : value >= 1_000_000 ? `${Math.round(value / 1_000_000)} MB` : value >= 1_000 ? `${Math.round(value / 1_000)} KB` : `${value} B`
const percent = () => task.value?.total_bytes ? Math.min(100, Math.round(task.value.downloaded_bytes / task.value.total_bytes * 100)) : 0
const statusLabel: Record<string, string> = { SCANNING: '正在扫描', DOWNLOADING: '正在下载', RETRYING: '等待自动重试', WAITING_RATE_LIMIT: 'Telegram 限流等待', PAUSED: '已暂停', COMPLETED: '已完成', ARCHIVE_INCOMPLETE: '归档不完整', DELETING: '删除处理中', FAILED: '失败', PARTIAL_FAILED: '部分失败', QUEUED: '等待中', CANCELLED: '已取消', PENDING: '等待下载', RETRY_WAIT: '等待重试', DELETED: '已删除' }
const mediaTypeLabel: Record<string, string> = { PHOTO: '图片', VIDEO: '视频', AUDIO: '音频', DOCUMENT: '文件' }
const totalPages = computed(() => mediaPage.value ? Math.max(1, Math.ceil(mediaPage.value.total / mediaPage.value.pageSize)) : 1)
const activeTransfers = computed(() => task.value?.activeMedia ?? [])
const runtime = computed(() => task.value?.downloadRuntime)
const retryMessage = (item: TaskMedia) => retryCountdown(item.status, item.next_retry_at, retryClock.value)
const thumbnailSource = (item: TaskMedia) => apiResourceUrl(taskMediaThumbnail(item, failedThumbnailIds.value.has(item.id)))
const thumbnailFallback = (item: TaskMedia) => taskMediaFallback(item.media_type)
const canPreview = (item: TaskMedia) => taskMediaPreview(item, failedThumbnailIds.value.has(item.id)) !== null
const viewerItems = computed(() => mediaPage.value?.items.filter((item) => canPreview(item)) ?? [])
const selectedCurrentMedia = computed(() => selectedMedia.value ? mediaPage.value?.items.find((item) => item.id === selectedMedia.value?.id) ?? selectedMedia.value : null)
const selectedPreview = computed(() => selectedCurrentMedia.value ? taskMediaPreview(selectedCurrentMedia.value, failedThumbnailIds.value.has(selectedCurrentMedia.value.id)) : null)
const selectedIndex = computed(() => selectedMedia.value ? viewerItems.value.findIndex((item) => item.id === selectedMedia.value?.id) : -1)
const viewerItem = computed<MediaViewerItem | null>(() => {
  const item = selectedCurrentMedia.value
  const preview = selectedPreview.value
  if (!item) return null
  return { id: `task-${item.id}`, filename: item.filename, mediaType: item.media_type, mimeType: item.mime_type, sizeBytes: item.size_bytes, messageDate: item.message_date, chatTitle: task.value?.chat_title, destinationName: task.value?.destination?.name ?? '未指定目的地', sourceLabel: `${mediaTypeLabel[item.media_type] || item.media_type} · 任务归档`, contentUrl: preview ? apiResourceUrl(preview.content_url) : null, downloadUrl: preview?.download_url ? apiResourceUrl(preview.download_url) : null }
})
const selectedCount = computed(() => selectedMediaIds.value.size)
const deletableLoadedCount = computed(() => mediaPage.value?.items.filter((item) => item.deletable).length ?? 0)
const allLoadedSelected = computed(() => deletableLoadedCount.value > 0 && selectedCount.value === deletableLoadedCount.value && [...selectedMediaIds.value].every((id) => mediaPage.value?.items.some((item) => item.id === id && item.deletable)))
const viewerDeleteTarget = computed(() => selectedCurrentMedia.value?.deletable ? { kind: 'task-media' as const, id: selectedCurrentMedia.value.id } : null)
const viewerError = computed(() => selectedMedia.value && !selectedPreview.value ? '此媒体当前没有可用的预览。' : null)

function markThumbnailFailed(item: TaskMedia) { failedThumbnailIds.value = new Set(failedThumbnailIds.value).add(item.id) }
function openMediaPreview(item: TaskMedia) { if (canPreview(item)) selectedMedia.value = item }
function closeMediaPreview() { selectedMedia.value = null }
function toggleMedia(id: number) {
  const item = mediaPage.value?.items.find((entry) => entry.id === id)
  if (!item?.deletable || deleting.value) return
  const next = new Set(selectedMediaIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedMediaIds.value = next
}
function pruneSelection() {
  const available = new Set((mediaPage.value?.items ?? []).filter((item) => item.deletable).map((item) => item.id))
  selectedMediaIds.value = new Set([...selectedMediaIds.value].filter((id) => available.has(id)))
}
function selectAllLoaded() {
  if (allLoadedSelected.value) selectedMediaIds.value = new Set()
  else selectedMediaIds.value = new Set(mediaPage.value?.items.filter((item) => item.deletable).map((item) => item.id) ?? [])
}
function requestViewerDelete() {
  const item = selectedCurrentMedia.value
  if (!item?.deletable || deleting.value) return
  deleteError.value = ''
  selectedMediaIds.value = new Set([item.id])
  confirmDeleteOpen.value = true
}
function requestSelectedDelete() { if (selectedCount.value && !deleting.value) confirmDeleteOpen.value = true }
function navigateMedia(direction: -1 | 1) {
  const next = viewerItems.value[selectedIndex.value + direction]
  if (next) openMediaPreview(next)
}
const eta = computed(() => {
  if (!task.value || !['DOWNLOADING', 'RETRYING'].includes(task.value.status)) return '—'
  if (task.value.speed_bytes_per_second <= 0) return '计算中'
  const seconds = Math.max(0, Math.ceil((task.value.total_bytes - task.value.downloaded_bytes) / task.value.speed_bytes_per_second))
  if (seconds < 60) return '少于 1 分钟'
  if (seconds < 3600) return `约 ${Math.ceil(seconds / 60)} 分钟`
  return `约 ${Math.floor(seconds / 3600)} 小时 ${Math.ceil(seconds % 3600 / 60)} 分钟`
})

async function loadMedia(page = mediaPage.value?.page ?? 1) {
  mediaPage.value = await api.taskMedia(taskId, page)
  pruneSelection()
}
async function load() {
  try {
    const [nextTask, nextMedia] = await Promise.all([api.task(taskId), api.taskMedia(taskId)])
    task.value = nextTask
    mediaPage.value = nextMedia
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法读取任务' }
}
function updateMedia(item: TaskMedia) {
  if (!mediaPage.value) return
  const index = mediaPage.value.items.findIndex((entry) => entry.id === item.id)
  if (index >= 0) {
    mediaPage.value.items.splice(index, 1)
    if (mediaPage.value.page === 1 && item.status === 'DOWNLOADING') mediaPage.value.items.unshift(item)
    else mediaPage.value.items.splice(index, 0, item)
    pruneSelection()
    return
  }
  if (mediaPage.value.page === 1 && (item.status === 'DOWNLOADING' || item.status === 'FAILED')) mediaPage.value.items.unshift(item)
  pruneSelection()
}
function connectEvents(afterRevision: number) {
  source?.close()
  source = new EventSource(api.eventsUrl(taskId, afterRevision))
  source.onopen = () => { streamState.value = 'connected' }
  source.onerror = () => { streamState.value = 'reconnecting' }
  source.addEventListener('task', (event) => {
    const nextTask = JSON.parse((event as MessageEvent).data) as Task
    task.value = nextTask
    if (mediaPage.value && mediaPage.value.total !== nextTask.total_count && !mediaSyncInFlight) {
      mediaSyncInFlight = true
      void loadMedia(mediaPage.value.page).finally(() => { mediaSyncInFlight = false })
    }
  })
  source.addEventListener('media', (event) => { updateMedia(JSON.parse((event as MessageEvent).data)) })
}
async function changePage(page: number) {
  if (page < 1 || page > totalPages.value) return
  selectedMediaIds.value = new Set()
  try { await loadMedia(page) } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法读取文件清单' }
}
async function action(kind: 'pause' | 'resume' | 'retry' | 'cancel') {
  if (kind === 'cancel' && !confirmCancel.value) { confirmCancel.value = true; return }
  try { task.value = await api.action(taskId, kind); confirmCancel.value = false; await loadMedia() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '无法更新任务' }
}
function delay(ms: number) { return new Promise((resolve) => window.setTimeout(resolve, ms)) }
async function waitForDelete(operation: DeleteOperation) {
  let current = operation
  while (current.status === 'PENDING' || current.status === 'RUNNING') {
    await delay(650)
    current = await api.deleteOperation(operation.operation_id)
    deleteOperation.value = current
  }
  return current
}
async function confirmDelete() {
  const ids = [...selectedMediaIds.value]
  if (!ids.length || deleting.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    const operation = await api.deleteTaskMedia(taskId, ids)
    deleteOperation.value = operation
    const finalOperation = operation.status === 'PENDING' || operation.status === 'RUNNING' ? await waitForDelete(operation) : operation
    deleteError.value = finalOperation.failed_count ? `${finalOperation.failed_count} 项删除失败，归档索引已保留，可在结果面板重试。` : ''
    if (finalOperation.success_count) closeMediaPreview()
    await Promise.all([loadMedia(mediaPage.value?.page ?? 1), api.task(taskId).then((nextTask) => { task.value = nextTask })])
    selectedMediaIds.value = new Set()
  } catch (reason) { deleteError.value = reason instanceof Error ? reason.message : '删除媒体失败'; error.value = deleteError.value }
  finally { deleting.value = false; confirmDeleteOpen.value = false }
}
async function retryDelete() {
  if (!deleteOperation.value || deleting.value) return
  deleting.value = true
  deleteError.value = ''
  try {
    const operation = await api.retryDeleteOperation(deleteOperation.value.operation_id)
    deleteOperation.value = operation
    const finalOperation = operation.status === 'PENDING' || operation.status === 'RUNNING' ? await waitForDelete(operation) : operation
    deleteError.value = finalOperation.failed_count ? `${finalOperation.failed_count} 项删除失败，归档索引已保留，可在结果面板重试。` : ''
    await loadMedia(mediaPage.value?.page ?? 1)
  } catch (reason) { deleteError.value = reason instanceof Error ? reason.message : '重试删除失败'; error.value = deleteError.value }
  finally { deleting.value = false }
}
onMounted(async () => { await load(); if (mediaPage.value) connectEvents(mediaPage.value.mediaRevision); retryClockTimer = window.setInterval(() => { retryClock.value = Date.now() }, 1000) })
onBeforeUnmount(() => { source?.close(); if (retryClockTimer !== null) window.clearInterval(retryClockTimer) })
</script>

<template>
  <div class="page-head"><div><RouterLink to="/tasks" class="back-link"><ArrowLeft :size="16" />返回任务中心</RouterLink><h1>{{ task?.chat_title || '任务详情' }}</h1><p class="subhead">{{ task?.chat_handle }} · 单聊天归档任务</p></div><span v-if="task" :class="['large-status', `status-${task.status.toLowerCase()}`]">{{ statusLabel[task.status] }}</span></div>
  <p v-if="error" class="form-error">{{ error }}</p>
  <template v-else-if="task">
    <section class="detail-progress"><div class="detail-progress-title"><div><span class="eyebrow">总体进度</span><strong>{{ percent() }}%</strong></div><div class="detail-controls"><button v-if="task.status === 'DOWNLOADING' || task.status === 'RETRYING'" class="quiet-button" @click="action('pause')"><CirclePause :size="17" />暂停</button><button v-if="task.status === 'PAUSED' || task.status === 'QUEUED'" class="primary-button" @click="action('resume')"><Play :size="17" />继续下载</button><button v-if="task.status === 'FAILED' || task.status === 'PARTIAL_FAILED'" class="primary-button" @click="action('retry')"><RefreshCw :size="17" />失败重试</button><button v-if="!['DELETING', 'COMPLETED', 'ARCHIVE_INCOMPLETE'].includes(task.status)" class="danger-button" @click="action('cancel')"><XCircle :size="17" />{{ confirmCancel ? '再次点击确认取消' : '取消任务' }}</button></div></div><div class="progress-track large"><i :style="{ width: `${percent()}%` }"></i></div><div class="metric-row"><div><span>已完成</span><b>{{ task.completed_count.toLocaleString() }} / {{ task.total_count.toLocaleString() }} 文件</b></div><div><span>已下载</span><b>{{ bytes(task.downloaded_bytes) }} / {{ bytes(task.total_bytes) }}</b></div><div><span>下载速度</span><b><Gauge :size="16" />{{ bytes(task.speed_bytes_per_second) }}/s</b></div><div><span>预计剩余</span><b><Clock3 :size="16" />{{ eta }}</b></div><div><span>当前传输</span><b>{{ activeTransfers.length }} / {{ runtime?.effectiveConcurrency ?? 0 }} 路 <small>（上限 {{ runtime?.maxConcurrency ?? 0 }}）</small></b></div></div></section>
    <div class="detail-grid"><section class="detail-panel"><h2>当前传输文件</h2><div v-if="activeTransfers.length" class="active-transfer-list"><p v-for="item in activeTransfers" :key="item.id" class="current-filename">{{ item.filename }} · {{ item.percent }}% · {{ bytes(item.speed_bytes_per_second) }}/s</p></div><p v-else class="current-filename">{{ task.status === 'WAITING_RATE_LIMIT' ? 'Telegram 限流等待中，暂不启动新文件' : '当前没有传输中的文件' }}</p><div class="log-list"><p>任务创建 · {{ new Date(task.created_at).toLocaleString('zh-CN') }}</p><p v-if="task.status === 'PAUSED'">任务已安全暂停，恢复后会重新安排未完成文件。</p><p v-else-if="task.status === 'WAITING_RATE_LIMIT'">{{ task.download_wait_until ? `预计 ${new Date(task.download_wait_until).toLocaleTimeString('zh-CN')} 后继续` : '等待 Telegram 允许继续下载' }}</p><p v-else-if="task.status === 'RETRYING'">文件发生临时错误，其他文件会继续；失败项将按退避时间自动重试。</p><p v-else-if="task.status === 'DELETING'">正在停止 worker 并删除任务归档，失败归档会保留并支持重试。</p><p v-else-if="task.status === 'DOWNLOADING'">正在写入临时文件，完成后将校验大小和内容哈希。</p><p v-else>任务状态：{{ statusLabel[task.status] }}</p></div></section><section class="detail-panel"><h2>归档筛选</h2><dl class="filter-summary"><div><dt>媒体类型</dt><dd>{{ Array.isArray(task.filters.media_types) ? task.filters.media_types.join(' · ') : '全部' }}</dd></div><div><dt>时间范围</dt><dd>{{ task.filters.date_start || '不限' }} 至 {{ task.filters.date_end || '不限' }}</dd></div><div><dt>文件大小</dt><dd>{{ task.filters.min_size_mb || 0 }} MB 至 {{ task.filters.max_size_mb || '不限' }} MB</dd></div></dl></section></div>
    <section class="task-media-panel"><div class="section-title"><div><h2>任务文件</h2><p>当前优先 · {{ mediaPage?.total ?? 0 }} 个文件</p></div><div class="section-title-actions"><span :class="['live-state', streamState]">{{ streamState === 'connected' ? '实时更新中' : '正在重连实时状态…' }}</span><div v-if="deletableLoadedCount" class="media-selection-actions"><button class="quiet-button" type="button" :disabled="deleting" @click="selectAllLoaded">{{ allLoadedSelected ? '取消全选' : `全选当前页（${deletableLoadedCount}）` }}</button><button v-if="selectedCount" class="danger-button" type="button" :disabled="deleting" @click="requestSelectedDelete"><Trash2 :size="16" />删除 {{ selectedCount }} 项</button></div></div></div>
      <div v-if="mediaPage?.items.length" class="task-media-list"><div v-for="item in mediaPage.items" :key="item.id" class="task-media-row" :class="{ selected: selectedMediaIds.has(item.id) }"><label v-if="item.deletable" class="selection-checkbox media-selection" @click.stop><input type="checkbox" :checked="selectedMediaIds.has(item.id)" :aria-label="`选择 ${item.filename}`" :disabled="deleting" @change="toggleMedia(item.id)" /><span></span></label><span v-else class="media-selection-placeholder" aria-hidden="true"></span><component :is="canPreview(item) ? 'button' : 'div'" :class="['task-media-thumbnail', `task-media-${thumbnailFallback(item)}`, { 'task-media-preview-trigger': canPreview(item) }]" :type="canPreview(item) ? 'button' : undefined" :aria-label="canPreview(item) ? `预览 ${item.filename}` : undefined" :aria-hidden="canPreview(item) ? undefined : 'true'" @click="canPreview(item) ? openMediaPreview(item) : undefined"><img v-if="thumbnailSource(item)" :src="thumbnailSource(item) ?? undefined" alt="" loading="lazy" decoding="async" @error="markThumbnailFailed(item)" /><template v-else><Image v-if="thumbnailFallback(item) === 'image'" :size="25" /><Video v-else-if="thumbnailFallback(item) === 'video'" :size="25" /><FileAudio v-else-if="thumbnailFallback(item) === 'audio'" :size="25" /><FileText v-else :size="25" /></template><span v-if="item.media_type === 'VIDEO' && thumbnailSource(item)" class="task-media-play-badge">▶</span></component><div class="task-media-name"><b>{{ item.filename }}</b><small>{{ mediaTypeLabel[item.media_type] || item.media_type }} · {{ statusLabel[item.status] || item.status }}</small><small v-if="item.error_message" class="media-error">{{ item.error_message }}</small><small v-if="retryMessage(item)" class="media-retry">{{ retryMessage(item) }}</small><small v-if="item.status === 'RETRY_WAIT' && item.attempt_count">自动重试 {{ item.attempt_count }} / 3 次</small></div><div class="task-media-progress"><div class="media-progress-label"><span>{{ bytes(item.downloaded_bytes) }} / {{ bytes(item.size_bytes) }}</span><span>{{ item.percent }}%</span></div><div class="progress-track"><i :style="{ width: `${item.percent}%` }"></i></div></div><span class="task-media-speed">{{ item.status === 'DOWNLOADING' ? `${bytes(item.speed_bytes_per_second)}/s` : item.status === 'FAILED' ? '失败' : item.status === 'COMPLETED' ? '已完成' : item.status === 'DELETED' ? '已删除' : item.status === 'RETRY_WAIT' ? '待重试' : '等待中' }}</span></div></div><p v-else class="task-media-empty">尚未建立文件清单。</p><footer v-if="mediaPage && totalPages > 1" class="task-media-pagination"><button class="quiet-button" :disabled="mediaPage.page === 1" @click="changePage(mediaPage.page - 1)">上一页</button><span>第 {{ mediaPage.page }} / {{ totalPages }} 页</span><button class="quiet-button" :disabled="mediaPage.page === totalPages" @click="changePage(mediaPage.page + 1)">下一页</button></footer>
    </section>
    <section v-if="task.failed_count" class="failed-panel"><FileWarning :size="20" /><div><h2>有 {{ task.failed_count }} 个文件需要处理</h2><p>网络错误会自动重试；仍失败的文件可从此任务再次加入队列。</p></div><button class="quiet-button" @click="action('retry')">重试失败项</button></section>
  </template>
  <DeleteResultPanel :operation="deleteOperation" :busy="deleting" @retry="retryDelete" @close="deleteOperation = null" />
  <DeleteConfirmDialog :open="confirmDeleteOpen" :count="selectedCount" subject="媒体文件" :busy="deleting" @cancel="confirmDeleteOpen = false" @confirm="confirmDelete" />
  <MediaViewer :open="Boolean(selectedMedia)" :item="viewerItem" :error="viewerError" :delete-error="deleteError || null" :has-previous="selectedIndex > 0" :has-next="selectedIndex >= 0 && selectedIndex < viewerItems.length - 1" :delete-target="viewerDeleteTarget" :deleting="deleting" @close="closeMediaPreview" @previous="navigateMedia(-1)" @next="navigateMedia(1)" @delete="requestViewerDelete" />
</template>
