<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowLeft, ChevronLeft, ChevronRight, Download, Info, PauseCircle, PlayCircle, RotateCw, Trash2, X } from 'lucide-vue-next'

export type MediaViewerItem = {
  id: string | number
  filename: string
  mediaType: string
  mimeType?: string | null
  sizeBytes?: number | null
  messageDate?: string | null
  chatTitle?: string | null
  destinationName?: string | null
  sourceLabel?: string | null
  contentUrl?: string | null
  downloadUrl?: string | null
}

const props = withDefaults(defineProps<{
  open: boolean
  item: MediaViewerItem | null
  loading?: boolean
  error?: string | null
  statusDetail?: string | null
  hasPrevious?: boolean
  hasNext?: boolean
  deletable?: boolean
}>(), {
  loading: false,
  error: null,
  statusDetail: null,
  hasPrevious: false,
  hasNext: false,
  deletable: false,
})

const emit = defineEmits<{
  close: []
  previous: []
  next: []
}>()

const viewerRoot = ref<HTMLElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
const infoOpen = ref(false)
const slideshow = ref(false)
const rotation = ref(0)
const mediaFailed = ref(false)
let slideshowTimer: number | null = null
let restoreFocus: HTMLElement | null = null
let previousBodyOverflow = ''

const typeLabel: Record<string, string> = { PHOTO: '图片', VIDEO: '视频', AUDIO: '音频', DOCUMENT: '文件' }
const isImage = computed(() => props.item?.mediaType === 'PHOTO' || props.item?.mimeType?.startsWith('image/') === true)
const isVideo = computed(() => props.item?.mediaType === 'VIDEO' || props.item?.mimeType?.startsWith('video/') === true)
const canRotate = computed(() => isImage.value)
const hasDownload = computed(() => Boolean(props.item?.downloadUrl))
const mediaReady = computed(() => Boolean(props.item?.contentUrl) && !props.loading && !props.error && !mediaFailed.value)
const fallbackMessage = computed(() => {
  if (props.error) return props.error
  if (props.loading) return '正在加载媒体…'
  if (mediaFailed.value) return '此媒体无法在当前浏览器中预览，请尝试下载原文件。'
  return '此媒体没有可用的浏览器预览。'
})

function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined) return '未知大小'
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)} GB`
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 100_000_000 ? 0 : 1)} MB`
  if (value >= 1_000) return `${Math.max(1, Math.round(value / 1_000))} KB`
  return `${value} B`
}

function formatDate(value: string | null | undefined) {
  if (!value) return '未知时间'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function stopSlideshow() {
  if (slideshowTimer !== null) window.clearInterval(slideshowTimer)
  slideshowTimer = null
}

function lockPageScroll() {
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
}

function unlockPageScroll() {
  document.body.style.overflow = previousBodyOverflow
}

function startSlideshow() {
  stopSlideshow()
  if (!props.hasNext) {
    slideshow.value = false
    return
  }
  slideshowTimer = window.setInterval(() => {
    if (props.hasNext) emit('next')
    else {
      slideshow.value = false
      stopSlideshow()
    }
  }, 4500)
}

function toggleSlideshow() {
  slideshow.value = !slideshow.value
  if (slideshow.value) startSlideshow()
  else stopSlideshow()
}

function requestClose() {
  slideshow.value = false
  stopSlideshow()
  restoreFocus?.focus()
  restoreFocus = null
  emit('close')
}

function goPrevious() {
  if (props.hasPrevious) emit('previous')
}

function goNext() {
  if (props.hasNext) emit('next')
}

function trapTab(event: KeyboardEvent) {
  const focusable = Array.from(viewerRoot.value?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], video[controls], iframe') ?? [])
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Tab') {
    trapTab(event)
  } else if (event.key === 'Escape') {
    event.preventDefault()
    requestClose()
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault()
    goPrevious()
  } else if (event.key === 'ArrowRight') {
    event.preventDefault()
    goNext()
  } else if (event.key === ' ' && !isVideo.value) {
    event.preventDefault()
    toggleSlideshow()
  }
}

watch(() => props.open, async (open) => {
  if (!open) {
    slideshow.value = false
    stopSlideshow()
    unlockPageScroll()
    return
  }
  lockPageScroll()
  restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  await nextTick()
  closeButton.value?.focus()
})

watch(() => props.item?.id, () => {
  rotation.value = 0
  mediaFailed.value = false
  if (slideshow.value) startSlideshow()
})

watch(() => props.hasNext, (hasNext) => {
  if (slideshow.value && !hasNext) {
    slideshow.value = false
    stopSlideshow()
  }
})

onBeforeUnmount(() => {
  stopSlideshow()
  unlockPageScroll()
})
</script>

<template>
  <section v-if="open && item" ref="viewerRoot" class="media-viewer" role="dialog" aria-modal="true" :aria-label="`${item.filename} 预览`" tabindex="-1" @keydown="onKeydown" @click.self="requestClose">
    <header class="media-viewer-topbar">
      <button ref="closeButton" class="media-viewer-icon" type="button" aria-label="退出大图查看" title="退出" @click="requestClose"><ArrowLeft :size="24" /></button>
      <div class="media-viewer-heading" aria-live="polite"><strong>{{ item.filename }}</strong><small>{{ item.sourceLabel || typeLabel[item.mediaType] || item.mediaType }}</small></div>
      <nav class="media-viewer-toolbar" aria-label="媒体工具">
        <button class="media-viewer-icon" type="button" :class="{ active: infoOpen }" :aria-expanded="infoOpen" aria-label="查看信息" title="信息" @click="infoOpen = !infoOpen"><Info :size="21" /></button>
        <button class="media-viewer-icon" type="button" :disabled="!deletable" :aria-label="deletable ? '删除媒体' : '删除功能暂未开放'" :title="deletable ? '删除' : '删除功能暂未开放'"><Trash2 :size="21" /></button>
        <a v-if="hasDownload" class="media-viewer-icon" :href="item.downloadUrl ?? undefined" aria-label="下载原文件" title="下载"><Download :size="21" /></a>
        <button v-else class="media-viewer-icon" type="button" disabled aria-label="当前媒体不可直接下载" title="当前媒体不可直接下载"><Download :size="21" /></button>
        <button class="media-viewer-icon" type="button" :disabled="!canRotate" :aria-label="canRotate ? '旋转图片' : '当前媒体不可旋转'" :title="canRotate ? '旋转' : '当前媒体不可旋转'" @click="rotation = (rotation + 90) % 360"><RotateCw :size="21" /></button>
        <button class="media-viewer-icon" type="button" :disabled="!hasNext" :aria-pressed="slideshow" :aria-label="slideshow ? '暂停幻灯片' : '播放幻灯片'" :title="slideshow ? '暂停幻灯片' : '播放幻灯片'" @click="toggleSlideshow"><PauseCircle v-if="slideshow" :size="22" /><PlayCircle v-else :size="22" /></button>
      </nav>
    </header>

    <div class="media-viewer-body">
      <button class="media-viewer-nav media-viewer-nav-previous" type="button" :disabled="!hasPrevious" aria-label="上一张" title="上一张" @click="goPrevious"><ChevronLeft :size="34" /></button>
      <main class="media-viewer-stage" @click.self="requestClose">
        <div v-if="mediaReady" class="media-viewer-content">
          <img v-if="isImage" :src="item.contentUrl ?? undefined" :alt="item.filename" :style="{ transform: `rotate(${rotation}deg)` }" @error="mediaFailed = true" />
          <video v-else-if="isVideo" :src="item.contentUrl ?? undefined" controls playsinline @error="mediaFailed = true" />
          <iframe v-else :src="item.contentUrl ?? undefined" :title="item.filename" @error="mediaFailed = true" />
        </div>
        <div v-else class="media-viewer-fallback" role="status"><div class="media-viewer-fallback-icon"><RotateCw v-if="loading" class="media-viewer-spinner" :size="34" /><Info v-else :size="34" /></div><p>{{ fallbackMessage }}</p><small v-if="statusDetail">{{ statusDetail }}</small></div>
      </main>
      <button class="media-viewer-nav media-viewer-nav-next" type="button" :disabled="!hasNext" aria-label="下一张" title="下一张" @click="goNext"><ChevronRight :size="34" /></button>

      <aside v-if="infoOpen" class="media-viewer-info" aria-label="媒体信息">
        <div class="media-viewer-info-head"><h2>信息</h2><button class="media-viewer-info-close" type="button" aria-label="关闭信息" @click="infoOpen = false"><X :size="20" /></button></div>
        <dl>
          <div><dt>文件名</dt><dd>{{ item.filename }}</dd></div>
          <div><dt>类型</dt><dd>{{ typeLabel[item.mediaType] || item.mediaType }}<span v-if="item.mimeType"> · {{ item.mimeType }}</span></dd></div>
          <div><dt>时间</dt><dd>{{ formatDate(item.messageDate) }}</dd></div>
          <div><dt>大小</dt><dd>{{ formatBytes(item.sizeBytes) }}</dd></div>
          <div v-if="item.chatTitle"><dt>来源聊天</dt><dd>{{ item.chatTitle }}</dd></div>
          <div v-if="item.destinationName"><dt>归档目的地</dt><dd>{{ item.destinationName }}</dd></div>
        </dl>
        <p class="media-viewer-info-note">当前详情来自已有媒体索引，不包含未保存的设备、相机或地理位置数据。</p>
      </aside>
    </div>
  </section>
</template>
