<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Download, FileAudio, FileText, Image, Search, Video, X } from 'lucide-vue-next'
import { api, apiResourceUrl, type ArchiveItem } from '../api'

const chats = ref<{ id: string; title: string; item_count: number }[]>([])
const items = ref<ArchiveItem[]>([])
const selectedChat = ref('')
const selectedType = ref('')
const selected = ref<ArchiveItem | null>(null)
const mediaFailed = ref(false)
const loading = ref(true)
const groupedItems = computed(() => items.value.reduce<Record<string, ArchiveItem[]>>((groups, item) => { const month = item.message_date.slice(0, 7); (groups[month] ??= []).push(item); return groups }, {}))
const bytes = (value: number) => value >= 1_000_000 ? `${(value / 1_000_000).toFixed(value >= 1_000_000_000 ? 1 : 0)} ${value >= 1_000_000_000 ? 'GB' : 'MB'}` : `${value} B`
const mediaLabel: Record<string, string> = { PHOTO: '图片', VIDEO: '视频', AUDIO: '音频', DOCUMENT: '文件' }
const previewStatus: Record<string, string> = { PENDING: '等待生成预览', PROCESSING: '正在生成预览', FAILED: '预览生成失败', UNAVAILABLE: '预览不可用' }
const resource = (path: string | null) => apiResourceUrl(path)
const formattedDate = (value: string) => new Date(value).toLocaleDateString('zh-CN')
const cardLabel = (item: ArchiveItem) => `${mediaLabel[item.media_type]}：${item.filename}。归属聊天：${item.chat_title}。${formattedDate(item.message_date)}，${bytes(item.size_bytes)}。打开预览`
async function load() { loading.value = true; try { items.value = await api.archiveMedia({ ...(selectedChat.value ? { chat_id: selectedChat.value } : {}), ...(selectedType.value ? { media_type: selectedType.value } : {}) }) } finally { loading.value = false } }
async function select(item: ArchiveItem) { mediaFailed.value = false; selected.value = await api.archiveDetail(item.id) }
function close() { selected.value = null; mediaFailed.value = false }
function onKeydown(event: KeyboardEvent) { if (event.key === 'Escape') close() }
onMounted(async () => { window.addEventListener('keydown', onKeydown); chats.value = await api.archiveChats(); await load() })
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="page-head"><div><p class="eyebrow">已落盘媒体</p><h1>归档文件</h1><p class="subhead">按来源聊天与月份回找已完成的归档内容。</p></div></div>
  <section class="archive-toolbar"><label class="select-wrap"><span>来源聊天</span><select v-model="selectedChat" @change="load"><option value="">全部来源</option><option v-for="chat in chats" :key="chat.id" :value="chat.id">{{ chat.title }} · {{ chat.item_count }} 个文件</option></select></label><div class="type-filters"><button v-for="type in ['', 'PHOTO', 'VIDEO', 'DOCUMENT']" :key="type" :class="{ selected: selectedType === type }" @click="selectedType = type; load()">{{ type ? mediaLabel[type] : '全部类型' }}</button></div></section>
  <section v-if="loading" class="loading-block">正在读取归档索引…</section><section v-else-if="!items.length" class="empty-state compact"><div class="empty-icon"><Search :size="26" /></div><h2>没有匹配的归档文件</h2><p>完成下载后的图片和视频会在这里生成缩略图。</p></section><section v-else class="archive-content"><div v-for="(monthItems, month) in groupedItems" :key="month" class="archive-month"><div class="section-title"><h2>{{ month }}</h2><span>{{ monthItems.length }} 个文件</span></div><div class="archive-grid"><button v-for="item in monthItems" :key="item.id" class="archive-card" :aria-label="cardLabel(item)" @click="select(item)"><div :class="['media-preview', item.media_type.toLowerCase()]"><img v-if="item.thumbnail_url" :src="resource(item.thumbnail_url) ?? undefined" alt="" /><template v-else><Image v-if="item.media_type === 'PHOTO'" :size="34" aria-hidden="true" /><Video v-else-if="item.media_type === 'VIDEO'" :size="34" aria-hidden="true" /><FileAudio v-else-if="item.media_type === 'AUDIO'" :size="34" aria-hidden="true" /><FileText v-else :size="34" aria-hidden="true" /><small v-if="previewStatus[item.thumbnail_status]" class="sr-only">{{ previewStatus[item.thumbnail_status] }}</small></template><span v-if="item.media_type === 'VIDEO'" class="play-badge" aria-hidden="true">▶</span><span class="archive-card-overlay" aria-hidden="true"><b>{{ item.filename }}</b><span class="archive-card-chat">{{ item.chat_title }}</span><small>{{ formattedDate(item.message_date) }} · {{ bytes(item.size_bytes) }}</small></span></div></button></div></div></section>
  <section v-if="selected" class="media-lightbox" role="dialog" aria-modal="true" :aria-label="`${selected.filename} 预览`" @click.self="close"><button class="lightbox-close" aria-label="关闭预览" @click="close"><X :size="24" /></button><div class="lightbox-panel"><div class="lightbox-media"><img v-if="selected.media_type === 'PHOTO' && selected.content_url && !mediaFailed" :src="resource(selected.content_url) ?? undefined" :alt="selected.filename" @error="mediaFailed = true" /><video v-else-if="selected.media_type === 'VIDEO' && selected.content_url && !mediaFailed" :src="resource(selected.content_url) ?? undefined" controls playsinline @error="mediaFailed = true" /><div v-else class="lightbox-fallback"><Image v-if="selected.media_type === 'PHOTO'" :size="46" /><Video v-else-if="selected.media_type === 'VIDEO'" :size="46" /><FileText v-else :size="46" /><p>{{ selected.media_type === 'VIDEO' && mediaFailed ? '此视频无法在当前浏览器播放' : '此文件没有可用的浏览器预览' }}</p></div></div><div class="lightbox-meta"><div><span class="eyebrow">{{ mediaLabel[selected.media_type] }} · 已归档</span><h2>{{ selected.filename }}</h2><p>{{ selected.chat_title }} · {{ new Date(selected.message_date).toLocaleDateString('zh-CN') }} · {{ bytes(selected.size_bytes) }}</p></div><a class="quiet-button" :href="resource(selected.download_url) ?? undefined"><Download :size="17" />下载原文件</a></div></div></section>
</template>
