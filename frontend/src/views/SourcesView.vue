<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Archive, Check, ChevronDown, ChevronRight, ChevronUp, Download, FileAudio, FileText, Image, LoaderCircle, Play, RefreshCw, Search, Video, X } from 'lucide-vue-next'
import { api, apiResourceUrl, type Chat, type ChatListSnapshot, type Destination, type SourceMedia, type SourcePreview } from '../api'
import { clearChatCache, readChatCache, writeChatCache } from '../chatCache'
import { isCurrentSourceRequest, retainSourceChatSelection, shouldPreloadSourceMedia } from '../sourcePreload'
import MediaViewer, { type MediaViewerItem } from '../components/MediaViewer.vue'

const chats = ref<Chat[]>([])
const chatSnapshot = ref<ChatListSnapshot | null>(null)
const destinations = ref<Destination[]>([])
const selectedDestinationId = ref<number | null>(null)
const router = useRouter()
const selectedChatId = ref('')
const search = ref('')
const mediaType = ref('')
const dateStart = ref('')
const dateEnd = ref('')
const items = ref<SourceMedia[]>([])
const nextCursor = ref<string | null>(null)
const loadingSources = ref(false)
const refreshingChats = ref(false)
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const sourcesError = ref('')
const mediaError = ref('')
const selected = ref(new Map<number, SourceMedia>())
const previewItem = ref<SourceMedia | null>(null)
const preview = ref<SourcePreview | null>(null)
const previewRequestError = ref('')
const successTaskId = ref<number | null>(null)
const sourcePanel = ref<'media' | 'sources' | 'selection'>('sources')
const filtersExpanded = ref(false)
const mediaScroll = ref<HTMLElement | null>(null)
const loadSentinel = ref<HTMLElement | null>(null)
let previewTimer: number | null = null
let thumbnailTimer: number | null = null
let loadObserver: IntersectionObserver | null = null
let requestEpoch = 0
let previewRequestId = 0

const sourceChats = computed(() => chats.value.filter((chat) => ['CHANNEL', 'GROUP'].includes(chat.type)))
const filteredChats = computed(() => sourceChats.value.filter((chat) => `${chat.title} ${chat.handle ?? ''}`.toLowerCase().includes(search.value.toLowerCase())))
const currentChat = computed(() => sourceChats.value.find((chat) => chat.id === selectedChatId.value) ?? null)
const selectedItems = computed(() => Array.from(selected.value.values()))
const selectedBytes = computed(() => selectedItems.value.reduce((total, item) => total + item.size_bytes, 0))
const groupedItems = computed(() => items.value.reduce<Record<string, SourceMedia[]>>((groups, item) => {
  const day = new Date(item.message_date).toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'short' })
  ;(groups[day] ??= []).push(item)
  return groups
}, {}))

const bytes = (value: number) => value >= 1_000_000_000 ? `${(value / 1_000_000_000).toFixed(1)} GB` : value >= 1_000_000 ? `${(value / 1_000_000).toFixed(value >= 100_000_000 ? 0 : 1)} MB` : `${Math.max(1, Math.round(value / 1_000))} KB`
const typeLabel: Record<string, string> = { PHOTO: '图片', VIDEO: '视频', AUDIO: '音频', DOCUMENT: '文件' }
const filterDestinationLabel = computed(() => destinations.value.find((destination) => destination.id === selectedDestinationId.value)?.name ?? '未指定')
const filterMediaLabel = computed(() => mediaType.value ? typeLabel[mediaType.value] || mediaType.value : '全部')
const filterDateLabel = computed(() => {
  if (!dateStart.value && !dateEnd.value) return '不限日期'
  return `${dateStart.value || '不限起始'} 至 ${dateEnd.value || '不限结束'}`
})
const resource = (path: string | null) => apiResourceUrl(path)
const viewerItems = computed(() => items.value.filter((item) => !item.archived && !item.queued))
const previewIndex = computed(() => previewItem.value ? viewerItems.value.findIndex((item) => item.message_id === previewItem.value?.message_id) : -1)
const previewViewerItem = computed<MediaViewerItem | null>(() => {
  const item = previewItem.value
  if (!item) return null
  const ready = preview.value?.status === 'READY' && preview.value.content_url
  return {
    id: `source-${item.message_id}`,
    filename: item.filename,
    mediaType: item.media_type,
    mimeType: item.mime_type,
    sizeBytes: item.size_bytes,
    messageDate: item.message_date,
    chatTitle: currentChat.value?.title,
    sourceLabel: `${typeLabel[item.media_type] || item.media_type} · 原文件预览`,
    contentUrl: ready ? resource(preview.value?.content_url ?? null) : null,
  }
})
const previewLoading = computed(() => Boolean(previewItem.value && (!preview.value || ['PENDING', 'DOWNLOADING'].includes(preview.value.status))))
const previewError = computed(() => previewRequestError.value || (preview.value?.status === 'FAILED' ? preview.value.error_message || '预览加载失败' : preview.value?.status === 'CONSUMED' ? '此预览已结束，请重新打开。' : null))
const previewStatusDetail = computed(() => preview.value && previewLoading.value ? `${bytes(preview.value.downloaded_bytes)} / ${bytes(preview.value.size_bytes)}；关闭会停止并保留断点 24 小时。` : null)
function itemIcon(item: SourceMedia) { return item.media_type === 'PHOTO' ? Image : item.media_type === 'VIDEO' ? Video : item.media_type === 'AUDIO' ? FileAudio : FileText }

function query(cursor?: string | null) {
  return {
    ...(cursor ? { cursor } : {}),
    ...(mediaType.value ? { media_type: mediaType.value } : {}),
    ...(dateStart.value ? { date_start: dateStart.value } : {}),
    ...(dateEnd.value ? { date_end: dateEnd.value } : {}),
    ...(selectedDestinationId.value ? { destination_id: String(selectedDestinationId.value) } : {}),
  }
}
function disconnectLoadObserver() {
  loadObserver?.disconnect()
  loadObserver = null
}
async function observeLoadSentinel(epoch = requestEpoch) {
  await nextTick()
  if (!isCurrentSourceRequest(epoch, requestEpoch)) return
  disconnectLoadObserver()
  if (!mediaScroll.value || !loadSentinel.value || !nextCursor.value || !('IntersectionObserver' in window)) return
  loadObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) void load(false)
  }, { root: mediaScroll.value, rootMargin: '0px 0px 200% 0px', threshold: 0 })
  loadObserver.observe(loadSentinel.value)
}
function mergeItems(fresh: SourceMedia[], previous: SourceMedia[]) {
  const byId = new Map(previous.map((item) => [item.message_id, item]))
  return [...fresh.map((item) => ({ ...byId.get(item.message_id), ...item })), ...previous.filter((item) => !fresh.some((next) => next.message_id === item.message_id))]
}
function stopThumbnailPolling() { if (thumbnailTimer !== null) window.clearInterval(thumbnailTimer); thumbnailTimer = null }
async function refreshThumbnailStatuses() {
  const pending = items.value.filter((item) => ['PENDING', 'DOWNLOADING'].includes(item.thumbnail_status)).slice(0, 50)
  if (!pending.length || !selectedChatId.value) { stopThumbnailPolling(); return }
  const chatId = selectedChatId.value
  try {
    const statuses = await api.sourceThumbnails(chatId, pending.map((item) => item.message_id))
    if (chatId !== selectedChatId.value) return
    items.value = items.value.map((item) => {
      const status = statuses[String(item.message_id)]
      return status ? { ...item, thumbnail_status: status.status, thumbnail_url: status.url } : item
    })
    if (!items.value.some((item) => ['PENDING', 'DOWNLOADING'].includes(item.thumbnail_status))) stopThumbnailPolling()
  } catch { stopThumbnailPolling() }
}
function startThumbnailPolling() { if (thumbnailTimer === null && items.value.some((item) => ['PENDING', 'DOWNLOADING'].includes(item.thumbnail_status))) { void refreshThumbnailStatuses(); thumbnailTimer = window.setInterval(() => void refreshThumbnailStatuses(), 1000) } }
async function refreshCachedPage(epoch: number, requestQuery: Record<string, string>, reset: boolean) {
  try {
    const page = await api.sourceMedia(selectedChatId.value, requestQuery, true)
    if (!isCurrentSourceRequest(epoch, requestEpoch)) return
    items.value = reset ? mergeItems(page.items, items.value) : mergeItems(items.value, page.items)
    nextCursor.value = page.next_cursor
    startThumbnailPolling()
  } catch { /* cached data remains usable; the primary request owns user-visible errors */ }
}
async function load(reset = true) {
  if (!selectedChatId.value) return
  if (!reset && !shouldPreloadSourceMedia({ nextCursor: nextCursor.value, loading: loading.value, loadingMore: loadingMore.value })) return

  const epoch = reset ? ++requestEpoch : requestEpoch
  const cursor = reset ? null : nextCursor.value
  const requestQuery = query(cursor)
  let receivedPage = false
  if (reset) {
    loading.value = true
    loadingMore.value = false
    items.value = []
    nextCursor.value = null
    mediaScroll.value?.scrollTo({ top: 0 })
    disconnectLoadObserver()
  } else {
    loadingMore.value = true
  }
  mediaError.value = ''
  try {
    const page = await api.sourceMedia(selectedChatId.value, requestQuery)
    if (!isCurrentSourceRequest(epoch, requestEpoch)) return
    items.value = reset ? page.items : [...items.value, ...page.items]
    nextCursor.value = page.next_cursor
    receivedPage = true
    startThumbnailPolling()
    if (page.cacheStatus === 'HIT') void refreshCachedPage(epoch, requestQuery, reset)
  } catch (reason) {
    if (isCurrentSourceRequest(epoch, requestEpoch)) mediaError.value = reason instanceof Error ? reason.message : '无法读取来源媒体'
  } finally {
    if (isCurrentSourceRequest(epoch, requestEpoch)) {
      loading.value = false
      loadingMore.value = false
      if (receivedPage) await observeLoadSentinel(epoch)
    }
  }
}
function clearSourceSelection() {
  selectedChatId.value = ''
  selected.value = new Map()
  items.value = []
  nextCursor.value = null
  mediaError.value = ''
  error.value = ''
  successTaskId.value = null
  previewItem.value = null
  preview.value = null
  previewRequestError.value = ''
  previewRequestId += 1
  loading.value = false
  loadingMore.value = false
  sourcePanel.value = 'sources'
  requestEpoch += 1
  disconnectLoadObserver()
  stopPolling()
  stopThumbnailPolling()
}
async function chooseChat(chat: Chat) {
  selectedChatId.value = chat.id
  selected.value = new Map()
  successTaskId.value = null
  sourcePanel.value = 'media'
  await load()
}
async function loadSources() {
  loadingSources.value = true
  sourcesError.value = ''
  try {
    applyChatSnapshot(await api.chats())
    const nextSelectedChatId = retainSourceChatSelection(selectedChatId.value, sourceChats.value.map((chat) => chat.id))
    if (nextSelectedChatId !== selectedChatId.value) clearSourceSelection()
    if (selectedChatId.value) await load()
  } catch (reason) {
    if (!clearUnavailableChats(reason)) sourcesError.value = reason instanceof Error ? reason.message : '无法读取群组与频道'
  } finally {
    loadingSources.value = false
  }
}
async function loadDestinations() {
  destinations.value = (await api.destinations()).filter((item) => item.enabled)
  if (!selectedDestinationId.value || !destinations.value.some((item) => item.id === selectedDestinationId.value)) selectedDestinationId.value = destinations.value[0]?.id ?? null
}
function applyChatSnapshot(snapshot: ChatListSnapshot) { chats.value = snapshot.chats; chatSnapshot.value = snapshot; writeChatCache(snapshot) }
function clearUnavailableChats(reason: unknown) {
  if ((reason as Error & { status?: number }).status !== 409) return false
  clearChatCache()
  chats.value = []
  chatSnapshot.value = null
  clearSourceSelection()
  return true
}
async function refreshChats() {
  refreshingChats.value = true
  sourcesError.value = ''
  try {
    applyChatSnapshot(await api.refreshChats())
    const nextSelectedChatId = retainSourceChatSelection(selectedChatId.value, sourceChats.value.map((chat) => chat.id))
    if (nextSelectedChatId !== selectedChatId.value) clearSourceSelection()
    if (selectedChatId.value) await load()
  } catch (reason) {
    const message = reason instanceof Error ? reason.message : '无法更新聊天列表'
    if (!clearUnavailableChats(reason)) {
      sourcesError.value = message
      if (chatSnapshot.value) applyChatSnapshot({ ...chatSnapshot.value, isStale: true, lastRefreshError: message })
    }
  } finally { refreshingChats.value = false }
}
function refreshedLabel() { return chatSnapshot.value?.refreshedAt ? new Date(chatSnapshot.value.refreshedAt).toLocaleString('zh-CN') : '尚未同步' }
function changeMediaType(type: string) {
  mediaType.value = type
  void load()
}
function changeDateFilter() { void load() }
function changeDestination() { selected.value = new Map(); void load() }
function toggle(item: SourceMedia) {
  if (item.archived || item.queued) return
  const next = new Map(selected.value)
  next.has(item.message_id) ? next.delete(item.message_id) : next.set(item.message_id, item)
  selected.value = next
}
function selectedState(item: SourceMedia) { return selected.value.has(item.message_id) }
function closePanelOnEscape(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  if (previewItem.value) { void closePreview(); return }
  if (sourcePanel.value !== 'media') sourcePanel.value = 'media'
}
function stopPolling() { if (previewTimer !== null) window.clearInterval(previewTimer); previewTimer = null }
async function refreshPreviewThumbnail(item: SourceMedia) {
  const chatId = selectedChatId.value
  try {
    const status = (await api.sourceThumbnails(chatId, [item.message_id]))[String(item.message_id)]
    if (!status || chatId !== selectedChatId.value) return
    items.value = items.value.map((current) => current.message_id === item.message_id ? { ...current, thumbnail_status: status.status, thumbnail_url: status.url } : current)
    item.thumbnail_status = status.status
    item.thumbnail_url = status.url
  } catch { /* original preview remains usable if its card cover cannot be refreshed */ }
}
function updatePreviewItem(next: SourcePreview) {
  preview.value = next
  if (previewItem.value) previewItem.value.preview = next
  if (next.status === 'READY' || next.status === 'FAILED') {
    stopPolling()
    if (next.status === 'READY' && previewItem.value) void refreshPreviewThumbnail(previewItem.value)
  }
}
async function openPreview(item: SourceMedia) {
  if (item.archived) { await router.push('/archives'); return }
  if (item.queued) return
  const requestId = ++previewRequestId
  const active = preview.value
  if (active && (active.status === 'PENDING' || active.status === 'DOWNLOADING')) void api.stopPreview(active.id).catch(() => undefined)
  previewRequestError.value = ''
  previewItem.value = item; preview.value = null; stopPolling()
  try {
    const next = await api.preview(selectedChatId.value, item)
    if (requestId !== previewRequestId || previewItem.value?.message_id !== item.message_id) {
      if (next.status === 'PENDING' || next.status === 'DOWNLOADING') void api.stopPreview(next.id).catch(() => undefined)
      return
    }
    updatePreviewItem(next)
    if (next.status === 'PENDING' || next.status === 'DOWNLOADING') {
      previewTimer = window.setInterval(async () => {
        if (requestId !== previewRequestId || previewItem.value?.message_id !== item.message_id || !preview.value) return
        try { updatePreviewItem(await api.previewStatus(preview.value.id)) } catch { stopPolling() }
      }, 900)
    }
  } catch (reason) {
    if (requestId === previewRequestId && previewItem.value?.message_id === item.message_id) {
      previewRequestError.value = reason instanceof Error ? reason.message : '无法加载预览'
      error.value = previewRequestError.value
    }
  }
}
function navigatePreview(direction: -1 | 1) {
  const next = viewerItems.value[previewIndex.value + direction]
  if (next) void openPreview(next)
}
async function closePreview() {
  previewRequestId += 1
  const active = preview.value
  stopPolling(); previewItem.value = null; preview.value = null; previewRequestError.value = ''
  if (active && (active.status === 'PENDING' || active.status === 'DOWNLOADING')) await api.stopPreview(active.id).catch(() => undefined)
}
async function queueSelection() {
  if (!currentChat.value || !selectedItems.value.length) return
  try {
    if (!selectedDestinationId.value) throw new Error('请先选择归档目的地')
    const task = await api.createSelectionTask({ chat_id: currentChat.value.id, chat_title: currentChat.value.title, chat_handle: currentChat.value.handle, message_ids: selectedItems.value.map((item) => item.message_id), destination_id: selectedDestinationId.value })
    const ids = new Set(selectedItems.value.map((item) => item.message_id))
    items.value = items.value.map((item) => ids.has(item.message_id) ? { ...item, queued: true } : item)
    selected.value = new Map(); successTaskId.value = task.id; sourcePanel.value = 'media'
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '加入下载队列失败' }
}
onMounted(async () => {
  window.addEventListener('keydown', closePanelOnEscape)
  const cached = readChatCache()
  if (cached) applyChatSnapshot(cached)
  try { await loadDestinations() } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法读取归档目的地' }
  await loadSources()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', closePanelOnEscape)
  requestEpoch += 1
  disconnectLoadObserver()
  stopPolling()
  stopThumbnailPolling()
})
</script>

<template>
  <div :class="['source-page', `source-panel-${sourcePanel}`]">
    <div v-if="sourcesError" class="form-error" role="alert">无法读取群组与频道：{{ sourcesError }} <button class="quiet-button" type="button" :disabled="loadingSources" @click="loadSources">{{ loadingSources ? '正在重试…' : '重试加载' }}</button></div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <p v-if="successTaskId" class="success-note source-success">已加入下载队列。<RouterLink :to="`/tasks/${successTaskId}`">查看任务详情</RouterLink></p>
    <section class="source-workbench">
      <aside class="source-list"><div class="chat-search-row"><label class="search-input"><Search :size="17" /><input v-model="search" placeholder="搜索聊天" /></label><button class="quiet-button icon-button" type="button" :disabled="refreshingChats" :aria-label="refreshingChats ? '正在刷新聊天列表' : '刷新聊天列表'" @click="refreshChats"><RefreshCw :class="{ spin: refreshingChats }" :size="16" /></button></div><p v-if="chatSnapshot?.isStale" class="chat-cache-notice">列表可能不是最新的{{ chatSnapshot.lastRefreshError ? `：${chatSnapshot.lastRefreshError}` : '' }}。上次同步：{{ refreshedLabel() }}</p><p class="source-count">{{ filteredChats.length }} 个来源</p><button v-for="chat in filteredChats" :key="chat.id" type="button" :class="['source-row', { selected: selectedChatId === chat.id }]" @click="chooseChat(chat)"><span class="source-avatar">{{ chat.title.slice(0, 1) }}</span><span><b>{{ chat.title }}</b><small>{{ chat.type === 'CHANNEL' ? '频道' : '群组' }}{{ chat.handle ? ` · ${chat.handle}` : '' }}</small></span><ChevronRight :size="15" /></button><p v-if="loadingSources" class="source-empty">正在读取群组与频道…</p><p v-else-if="!filteredChats.length" class="source-empty">没有可浏览的群组或频道。</p></aside>
      <main class="source-timeline">
        <template v-if="currentChat">
          <header class="source-title">
            <div><span class="eyebrow">{{ currentChat.type === 'CHANNEL' ? '频道' : '群组' }}</span><h2>{{ currentChat.title }}</h2></div>
            <div class="source-title-actions"><span>{{ items.length }} 项已载入</span><button class="quiet-button mobile-source-switch" type="button" @click="sourcePanel = 'sources'">切换来源</button></div>
          </header>
          <section class="source-filter-toolbar" aria-label="媒体筛选">
            <div class="source-filter-toolbar-head">
              <div class="source-filter-summary" aria-label="当前筛选条件">
                <span class="source-filter-summary-title">筛选</span>
                <span class="source-filter-summary-item"><b>目标</b><span>{{ filterDestinationLabel }}</span></span>
                <span class="source-filter-summary-item"><b>类型</b><span>{{ filterMediaLabel }}</span></span>
                <span class="source-filter-summary-item"><b>日期</b><span>{{ filterDateLabel }}</span></span>
              </div>
              <button class="source-filter-toggle" type="button" :aria-expanded="filtersExpanded" aria-controls="source-filter-panel" :aria-label="filtersExpanded ? '收起媒体筛选' : '展开媒体筛选'" @click="filtersExpanded = !filtersExpanded">
                <span>{{ filtersExpanded ? '收起筛选' : '展开筛选' }}</span>
                <ChevronUp v-if="filtersExpanded" :size="16" aria-hidden="true" /><ChevronDown v-else :size="16" aria-hidden="true" />
              </button>
            </div>
            <Transition name="source-filter-panel">
              <div v-if="filtersExpanded" id="source-filter-panel" class="source-filter-body">
                <div class="source-filters" role="region" aria-label="媒体筛选控件">
                  <fieldset class="filter-group filter-group-destination">
                    <legend>归档目标</legend>
                    <label class="filter-control filter-select-control">
                      <select v-model.number="selectedDestinationId" aria-label="归档目标" @change="changeDestination">
                        <option v-for="destination in destinations" :key="destination.id" :value="destination.id">{{ destination.name }}</option>
                      </select>
                    </label>
                  </fieldset>
                  <fieldset class="filter-group filter-group-media">
                    <legend>媒体类型</legend>
                    <div class="type-filters" role="group" aria-label="媒体类型">
                      <button v-for="type in ['', 'PHOTO', 'VIDEO', 'DOCUMENT']" :key="type" type="button" :class="{ selected: mediaType === type }" :aria-pressed="mediaType === type" @click="changeMediaType(type)">{{ type ? typeLabel[type] : '全部' }}</button>
                    </div>
                  </fieldset>
                  <fieldset class="filter-group filter-group-date">
                    <legend>日期范围</legend>
                    <div class="filter-date-fields">
                      <label class="filter-date-field"><span>开始</span><input v-model="dateStart" aria-label="开始日期" type="date" @change="changeDateFilter" /></label>
                      <span class="filter-date-separator" aria-hidden="true">至</span>
                      <label class="filter-date-field"><span>结束</span><input v-model="dateEnd" aria-label="结束日期" type="date" @change="changeDateFilter" /></label>
                    </div>
                  </fieldset>
                </div>
              </div>
            </Transition>
          </section>
          <section ref="mediaScroll" class="source-media-scroll">
            <section v-if="loading" class="loading-block">正在读取媒体时间流…</section>
            <section v-else-if="mediaError && !items.length" class="empty-state compact"><div class="empty-icon"><Archive :size="25" /></div><h2>媒体时间流加载失败</h2><p>{{ mediaError }}</p><button class="quiet-button" type="button" @click="load()">重试加载</button></section>
            <section v-else-if="!items.length" class="empty-state compact"><div class="empty-icon"><Archive :size="25" /></div><h2>没有匹配的媒体</h2><p>调整日期或类型筛选后重试。</p></section>
            <template v-else>
              <section v-for="(dayItems, day) in groupedItems" :key="day" class="source-day">
                <div class="section-title"><h2>{{ day }}</h2><span>{{ dayItems.length }} 项</span></div>
                <div class="source-grid">
                  <article v-for="item in dayItems" :key="item.message_id" :class="['source-card', { checked: selectedState(item), unavailable: item.archived || item.queued }]">
                    <button v-if="!item.archived && !item.queued" class="select-box" type="button" :aria-label="`选择 ${item.filename}`" @click="toggle(item)"><Check v-if="selectedState(item)" :size="15" /></button>
                    <button type="button" class="source-card-open" :disabled="item.queued" :aria-label="item.archived ? `在归档中查看 ${item.filename}` : item.queued ? `${item.filename} 已加入队列，暂不可预览` : `打开 ${item.filename} 的大图`" @click="openPreview(item)">
                      <div class="source-card-preview"><img v-if="item.thumbnail_url" :src="resource(item.thumbnail_url) ?? undefined" :alt="item.filename" /><component v-else :is="itemIcon(item)" :size="28" aria-hidden="true" /><span v-if="item.media_type === 'VIDEO'" class="play-badge" aria-hidden="true"><Play :size="12" fill="currentColor" /></span></div>
                      <div><b>{{ item.filename }}</b><small>{{ typeLabel[item.media_type] }} · {{ bytes(item.size_bytes) }}</small><em v-if="item.archived">已归档</em><em v-else-if="item.queued">已加入队列</em><em v-else-if="item.thumbnail_status === 'FAILED'">缩略图可重试</em></div>
                    </button>
                  </article>
                </div>
              </section>
              <div ref="loadSentinel" class="source-load-state"><template v-if="loadingMore"><LoaderCircle class="spin" :size="16" />正在预载更早的媒体…</template><template v-else-if="mediaError"><span>更早的媒体加载失败：{{ mediaError }}</span><button class="quiet-button" type="button" @click="load(false)">重试加载</button></template><template v-else-if="!nextCursor">已加载全部媒体</template></div>
            </template>
          </section>
        </template>
        <template v-else-if="sourceChats.length"><section class="empty-state compact"><div class="empty-icon"><Archive :size="25" /></div><h2>请选择一个群组或频道</h2><p>从左侧选择会话后加载媒体缩略图。</p><button class="quiet-button mobile-source-switch" type="button" @click="sourcePanel = 'sources'">选择来源</button></section></template>
        <section v-else class="empty-state compact"><div class="empty-icon"><Archive :size="25" /></div><h2>没有可用来源</h2><p>连接 Telegram 后会显示已加入的群组与频道。</p></section>
      </main>
      <aside class="selection-basket"><div class="selection-basket-head"><div><span class="eyebrow">待下载</span><h2>{{ selectedItems.length }} 项</h2></div><button class="quiet-button mobile-selection-close" type="button" aria-label="返回媒体列表" @click="sourcePanel = 'media'">返回</button></div><p>{{ selectedItems.length ? bytes(selectedBytes) : '从时间流中选择文件' }}</p><div v-if="selectedItems.length" class="basket-list"><button v-for="item in selectedItems" :key="item.message_id" type="button" @click="toggle(item)"><span>{{ item.filename }}</span><X :size="15" /></button></div><p v-else class="basket-empty">选择会跨日期和分页保留。</p><button class="primary-button wide" type="button" :disabled="!selectedItems.length" @click="queueSelection"><Download :size="17" />加入下载队列</button><small>文件按来源顺序加入统一下载队列。</small></aside>
    </section>
    <button v-if="selectedItems.length" class="selection-launcher primary-button" type="button" @click="sourcePanel = 'selection'"><span>已选 {{ selectedItems.length }} 项 · {{ bytes(selectedBytes) }}</span><span>查看并继续</span></button>
    <MediaViewer :open="Boolean(previewItem)" :item="previewViewerItem" :loading="previewLoading" :error="previewError" :status-detail="previewStatusDetail" :has-previous="previewIndex > 0" :has-next="previewIndex >= 0 && previewIndex < viewerItems.length - 1" @close="closePreview" @previous="navigatePreview(-1)" @next="navigatePreview(1)" />
  </div>
</template>
