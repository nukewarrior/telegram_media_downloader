<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type ComponentPublicInstance } from 'vue'
import { CalendarDays, Download, FileAudio, FileText, Filter, Image, Search, Video, X } from 'lucide-vue-next'
import { api, apiResourceUrl, type ArchiveItem } from '../api'

type ArchiveDay = { key: string; label: string; fullLabel: string; year: number; items: ArchiveItem[] }
type ArchiveMonth = { key: string; label: string; year: number; items: ArchiveItem[]; days: ArchiveDay[] }

const chats = ref<{ id: string; title: string; item_count: number }[]>([])
const items = ref<ArchiveItem[]>([])
const selectedChat = ref('')
const selectedType = ref('')
const selected = ref<ArchiveItem | null>(null)
const mediaFailed = ref(false)
const loading = ref(true)
const activeDayKey = ref('')
const hoveredDayKey = ref<string | null>(null)
const timeNavOpen = ref(false)
const mobileDateMenuOpen = ref(false)
const filterOpen = ref(false)
const isScrubbing = ref(false)
const dayAnchors = ref<Record<string, HTMLElement>>({})
const timeRail = ref<HTMLElement | null>(null)

const weekDays = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六']
const mediaLabel: Record<string, string> = { PHOTO: '图片', VIDEO: '视频', AUDIO: '音频', DOCUMENT: '文件' }
const previewStatus: Record<string, string> = { PENDING: '等待生成预览', PROCESSING: '正在生成预览', FAILED: '预览生成失败', UNAVAILABLE: '预览不可用' }
const bytes = (value: number) => value >= 1_000_000 ? `${(value / 1_000_000).toFixed(value >= 1_000_000_000 ? 1 : 0)} ${value >= 1_000_000_000 ? 'GB' : 'MB'}` : `${value} B`
const resource = (path: string | null) => apiResourceUrl(path)

function archiveDate(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? new Date(`${value.slice(0, 10)}T00:00:00`) : parsed
}

function dateParts(value: string) {
  const date = archiveDate(value)
  return { year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate(), weekday: date.getDay() }
}

const formattedDate = (value: string) => archiveDate(value).toLocaleDateString('zh-CN')
const cardLabel = (item: ArchiveItem) => `${mediaLabel[item.media_type]}：${item.filename}。归属聊天：${item.chat_title}。${formattedDate(item.message_date)}，${bytes(item.size_bytes)}。打开预览`

const archiveMonths = computed<ArchiveMonth[]>(() => {
  const months = new Map<string, ArchiveMonth>()
  for (const item of items.value) {
    const { year, month, day, weekday } = dateParts(item.message_date)
    const monthKey = `${year}-${String(month).padStart(2, '0')}`
    const dayKey = `${monthKey}-${String(day).padStart(2, '0')}`
    let monthGroup = months.get(monthKey)
    if (!monthGroup) {
      monthGroup = { key: monthKey, label: `${year}年${month}月`, year, items: [], days: [] }
      months.set(monthKey, monthGroup)
    }
    monthGroup.items.push(item)
    let dayGroup = monthGroup.days.find((group) => group.key === dayKey)
    if (!dayGroup) {
      dayGroup = { key: dayKey, label: `${month}月${day}日，${weekDays[weekday]}`, fullLabel: `${year}年${month}月${day}日`, year, items: [] }
      monthGroup.days.push(dayGroup)
    }
    dayGroup.items.push(item)
  }
  return [...months.values()]
    .sort((left, right) => right.key.localeCompare(left.key))
    .map((month) => ({ ...month, days: month.days.sort((left, right) => right.key.localeCompare(left.key)) }))
})

const archiveDays = computed(() => archiveMonths.value.flatMap((month) => month.days))
const focusedDayKey = computed(() => hoveredDayKey.value ?? activeDayKey.value)
const focusedDay = computed(() => archiveDays.value.find((day) => day.key === focusedDayKey.value) ?? archiveDays.value[0] ?? null)
const activeFilterCount = computed(() => Number(Boolean(selectedChat.value)) + Number(Boolean(selectedType.value)))
const archiveTypes = ['', 'PHOTO', 'VIDEO', 'AUDIO', 'DOCUMENT']

async function load() {
  loading.value = true
  try {
    items.value = await api.archiveMedia({ ...(selectedChat.value ? { chat_id: selectedChat.value } : {}), ...(selectedType.value ? { media_type: selectedType.value } : {}) })
  } finally {
    loading.value = false
  }
}

async function applyFilters() {
  filterOpen.value = false
  await load()
}

async function clearFilters() {
  if (!selectedChat.value && !selectedType.value) {
    filterOpen.value = false
    return
  }
  selectedChat.value = ''
  selectedType.value = ''
  await applyFilters()
}

async function select(item: ArchiveItem) {
  mediaFailed.value = false
  selected.value = await api.archiveDetail(item.id)
}

function close() {
  selected.value = null
  mediaFailed.value = false
}

function setDayAnchor(key: string, element: Element | ComponentPublicInstance | null) {
  const node = element instanceof HTMLElement
    ? element
    : element instanceof Element
      ? null
      : element?.$el instanceof HTMLElement
        ? element.$el
        : null
  if (node) dayAnchors.value[key] = node
  else delete dayAnchors.value[key]
}

function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

function scrollToDay(key: string, behavior: ScrollBehavior = 'smooth') {
  const target = dayAnchors.value[key]
  if (!target) return
  activeDayKey.value = key
  target.scrollIntoView({ block: 'start', behavior: prefersReducedMotion() ? 'auto' : behavior })
}

function updateActiveDay() {
  const days = archiveDays.value
  if (!days.length) return
  let current = days[0].key
  for (const day of days) {
    if (dayAnchors.value[day.key]?.getBoundingClientRect().top <= 154) current = day.key
    else break
  }
  activeDayKey.value = current
}

let scrollFrame: number | null = null
function onScroll() {
  if (scrollFrame !== null) return
  scrollFrame = window.requestAnimationFrame(() => {
    scrollFrame = null
    updateActiveDay()
  })
}

function openTimeNavigation() {
  timeNavOpen.value = true
  if (!hoveredDayKey.value) hoveredDayKey.value = activeDayKey.value
}

function closeTimeNavigation() {
  if (isScrubbing.value) return
  timeNavOpen.value = false
  hoveredDayKey.value = null
}

function dayPosition(index: number) {
  const days = archiveDays.value
  if (days.length <= 1) return '50%'
  return `${6 + (index / (days.length - 1)) * 88}%`
}

function updateTimelineTarget(clientY: number) {
  const rail = timeRail.value
  const days = archiveDays.value
  if (!rail || !days.length) return
  const bounds = rail.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (clientY - bounds.top - bounds.height * 0.06) / (bounds.height * 0.88)))
  hoveredDayKey.value = days[Math.round(ratio * (days.length - 1))].key
}

function onTimelinePointerMove(event: PointerEvent) {
  updateTimelineTarget(event.clientY)
  if (isScrubbing.value && focusedDayKey.value) scrollToDay(focusedDayKey.value, 'auto')
}

function onTimelinePointerDown(event: PointerEvent) {
  isScrubbing.value = true
  const target = event.currentTarget as HTMLElement
  target.setPointerCapture(event.pointerId)
  updateTimelineTarget(event.clientY)
  if (focusedDayKey.value) scrollToDay(focusedDayKey.value, 'auto')
}

function onTimelinePointerUp(event: PointerEvent) {
  isScrubbing.value = false
  const target = event.currentTarget as HTMLElement
  if (target.hasPointerCapture(event.pointerId)) target.releasePointerCapture(event.pointerId)
}

function onTimelineKeydown(event: KeyboardEvent) {
  const days = archiveDays.value
  const current = Math.max(0, days.findIndex((day) => day.key === focusedDayKey.value))
  let next = current
  if (event.key === 'ArrowDown') next = Math.min(days.length - 1, current + 1)
  else if (event.key === 'ArrowUp') next = Math.max(0, current - 1)
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = days.length - 1
  else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    if (focusedDayKey.value) scrollToDay(focusedDayKey.value)
    return
  } else return
  event.preventDefault()
  hoveredDayKey.value = days[next]?.key ?? null
}

function focusedDayIndex() {
  return Math.max(0, archiveDays.value.findIndex((day) => day.key === focusedDayKey.value))
}

function timelineScale(index: number) {
  const distance = Math.abs(index - focusedDayIndex())
  return Math.max(1, 2.15 - distance * 0.38)
}

function isYearBoundary(index: number) {
  return index === 0 || archiveDays.value[index - 1]?.year !== archiveDays.value[index]?.year
}

function showYearMarker(index: number) {
  return isYearBoundary(index) && Math.abs(index - focusedDayIndex()) > 2
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  if (selected.value) close()
  else if (filterOpen.value) filterOpen.value = false
  else if (mobileDateMenuOpen.value) mobileDateMenuOpen.value = false
  else if (timeNavOpen.value) closeTimeNavigation()
}

watch(archiveDays, async (days) => {
  activeDayKey.value = days[0]?.key ?? ''
  hoveredDayKey.value = null
  await nextTick()
  updateActiveDay()
})

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('scroll', onScroll, { passive: true })
  window.addEventListener('resize', updateActiveDay)
  chats.value = await api.archiveChats()
  await load()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('scroll', onScroll)
  window.removeEventListener('resize', updateActiveDay)
  if (scrollFrame !== null) window.cancelAnimationFrame(scrollFrame)
})
</script>

<template>
  <main class="archive-page">
    <div class="archive-filter-controls">
      <button class="archive-filter-trigger" :class="{ active: activeFilterCount }" :aria-expanded="filterOpen" aria-controls="archive-filter-panel" aria-label="筛选归档" title="筛选归档" @click="mobileDateMenuOpen = false; filterOpen = !filterOpen"><Filter :size="19" /><span v-if="activeFilterCount" class="archive-filter-count">{{ activeFilterCount }}</span></button>
      <button v-if="archiveDays.length" class="quiet-button archive-date-jump" :aria-expanded="mobileDateMenuOpen" aria-controls="archive-mobile-date-menu" @click="filterOpen = false; mobileDateMenuOpen = !mobileDateMenuOpen"><CalendarDays :size="17" />跳转日期</button>
    </div>
    <div v-if="filterOpen" class="archive-filter-backdrop" aria-hidden="true" @click="filterOpen = false"></div>
    <section v-if="filterOpen" id="archive-filter-panel" class="archive-filter-panel" role="dialog" aria-modal="false" aria-label="筛选归档">
      <div class="archive-filter-panel-head"><strong>筛选归档</strong><button class="archive-filter-close" aria-label="关闭筛选" @click="filterOpen = false"><X :size="18" /></button></div>
      <label class="archive-filter-field"><span>来源聊天</span><select v-model="selectedChat" @change="applyFilters"><option value="">全部来源</option><option v-for="chat in chats" :key="chat.id" :value="chat.id">{{ chat.title }} · {{ chat.item_count }} 个文件</option></select></label>
      <fieldset class="archive-filter-types"><legend>文件类型</legend><div><button v-for="type in archiveTypes" :key="type" :class="{ selected: selectedType === type }" @click="selectedType = type; applyFilters()">{{ type ? mediaLabel[type] : '全部' }}</button></div></fieldset>
      <button class="archive-filter-clear" :disabled="!activeFilterCount" @click="clearFilters">清除筛选</button>
    </section>
    <section v-if="mobileDateMenuOpen && archiveMonths.length" id="archive-mobile-date-menu" class="archive-mobile-date-menu" aria-label="按日期跳转"><div v-for="month in archiveMonths" :key="month.key" class="archive-mobile-month"><strong>{{ month.label }}</strong><div><button v-for="day in month.days" :key="day.key" :class="{ active: day.key === activeDayKey }" @click="scrollToDay(day.key); mobileDateMenuOpen = false">{{ day.label }}</button></div></div></section>
    <section v-if="loading" class="loading-block">正在读取归档索引…</section>
    <section v-else-if="!items.length" class="empty-state compact"><div class="empty-icon"><Search :size="26" /></div><h2>没有匹配的归档文件</h2><p>完成下载后的图片和视频会在这里生成缩略图。</p></section>
    <section v-else class="archive-scroll-region">
      <section class="archive-content">
        <section v-for="month in archiveMonths" :key="month.key" class="archive-month" :data-month-key="month.key">
          <div class="archive-month-heading"><h2>{{ month.label }}</h2><span>{{ month.items.length }} 个文件</span></div>
          <div v-for="day in month.days" :key="day.key" :ref="(element) => setDayAnchor(day.key, element)" class="archive-day" :data-day-key="day.key">
            <div class="archive-day-heading"><h3>{{ day.label }}</h3><span>{{ day.items.length }} 个文件</span></div>
            <div class="archive-grid"><button v-for="item in day.items" :key="item.id" class="archive-card" :aria-label="cardLabel(item)" @click="select(item)"><div :class="['media-preview', item.media_type.toLowerCase()]"><img v-if="item.thumbnail_url" :src="resource(item.thumbnail_url) ?? undefined" alt="" /><template v-else><Image v-if="item.media_type === 'PHOTO'" :size="34" aria-hidden="true" /><Video v-else-if="item.media_type === 'VIDEO'" :size="34" aria-hidden="true" /><FileAudio v-else-if="item.media_type === 'AUDIO'" :size="34" aria-hidden="true" /><FileText v-else :size="34" aria-hidden="true" /><small v-if="previewStatus[item.thumbnail_status]" class="sr-only">{{ previewStatus[item.thumbnail_status] }}</small></template><span v-if="item.media_type === 'VIDEO'" class="play-badge" aria-hidden="true">▶</span><span class="archive-card-overlay" aria-hidden="true"><b>{{ item.filename }}</b><span class="archive-card-chat">{{ item.chat_title }}</span><small>{{ formattedDate(item.message_date) }} · {{ bytes(item.size_bytes) }}</small></span></div></button></div>
          </div>
        </section>
      </section>
      <div v-if="archiveDays.length" :class="['archive-time-nav', { open: timeNavOpen }]" tabindex="0" role="navigation" aria-label="按日期跳转归档" :aria-expanded="timeNavOpen" @pointerenter="openTimeNavigation" @pointerleave="closeTimeNavigation" @pointermove="onTimelinePointerMove" @pointerdown="onTimelinePointerDown" @pointerup="onTimelinePointerUp" @pointercancel="onTimelinePointerUp" @focus="openTimeNavigation" @blur="closeTimeNavigation" @keydown="onTimelineKeydown">
        <div ref="timeRail" class="archive-time-rail" :aria-hidden="!timeNavOpen">
          <span v-if="focusedDay" class="archive-time-current-label" :style="{ top: dayPosition(focusedDayIndex()) }">{{ focusedDay.fullLabel }}</span>
          <span v-for="(day, index) in archiveDays" :key="day.key" class="archive-time-stop" :class="{ active: day.key === focusedDayKey, current: day.key === activeDayKey }" :style="{ top: dayPosition(index), '--timeline-scale': timelineScale(index) }"><span v-if="showYearMarker(index)" class="archive-time-year">{{ day.year }}年</span><i></i></span>
        </div>
      </div>
    </section>
    <section v-if="selected" class="media-lightbox" role="dialog" aria-modal="true" :aria-label="`${selected.filename} 预览`" @click.self="close"><button class="lightbox-close" aria-label="关闭预览" @click="close"><X :size="24" /></button><div class="lightbox-panel"><div class="lightbox-media"><img v-if="selected.media_type === 'PHOTO' && selected.content_url && !mediaFailed" :src="resource(selected.content_url) ?? undefined" :alt="selected.filename" @error="mediaFailed = true" /><video v-else-if="selected.media_type === 'VIDEO' && selected.content_url && !mediaFailed" :src="resource(selected.content_url) ?? undefined" controls playsinline @error="mediaFailed = true" /><div v-else class="lightbox-fallback"><Image v-if="selected.media_type === 'PHOTO'" :size="46" /><Video v-else-if="selected.media_type === 'VIDEO'" :size="46" /><FileText v-else :size="46" /><p>{{ selected.media_type === 'VIDEO' && mediaFailed ? '此视频无法在当前浏览器播放' : '此文件没有可用的浏览器预览' }}</p></div></div><div class="lightbox-meta"><div><span class="eyebrow">{{ mediaLabel[selected.media_type] }} · 已归档</span><h2>{{ selected.filename }}</h2><p>{{ selected.chat_title }} · {{ formattedDate(selected.message_date) }} · {{ bytes(selected.size_bytes) }}</p></div><a class="quiet-button" :href="resource(selected.download_url) ?? undefined"><Download :size="17" />下载原文件</a></div></div></section>
  </main>
</template>
