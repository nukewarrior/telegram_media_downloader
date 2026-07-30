<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Clipboard, File, FileAudio, FileText, Image, Info, Search, Video, X } from 'lucide-vue-next'
import { api, type ArchiveItem } from '../api'

const chats = ref<{ id: string; title: string; item_count: number }[]>([])
const items = ref<ArchiveItem[]>([])
const selectedChat = ref('')
const selectedType = ref('')
const selected = ref<ArchiveItem | null>(null)
const copied = ref(false)
const loading = ref(true)
const groupedItems = computed(() => items.value.reduce<Record<string, ArchiveItem[]>>((groups, item) => { const month = item.message_date.slice(0, 7); (groups[month] ??= []).push(item); return groups }, {}))
const bytes = (value: number) => value >= 1_000_000 ? `${(value / 1_000_000).toFixed(value >= 1_000_000_000 ? 1 : 0)} ${value >= 1_000_000_000 ? 'GB' : 'MB'}` : `${value} B`
const mediaLabel: Record<string, string> = { PHOTO: '图片', VIDEO: '视频', AUDIO: '音频', DOCUMENT: '文件' }
async function load() { loading.value = true; try { items.value = await api.archiveMedia({ ...(selectedChat.value ? { chat_id: selectedChat.value } : {}), ...(selectedType.value ? { media_type: selectedType.value } : {}) }) } finally { loading.value = false } }
async function select(item: ArchiveItem) { selected.value = await api.archiveDetail(item.id) }
async function copyPath() { if (!selected.value) return; await navigator.clipboard.writeText(selected.value.canonical_path); copied.value = true; window.setTimeout(() => copied.value = false, 1800) }
onMounted(async () => { chats.value = await api.archiveChats(); await load() })
</script>

<template>
  <div class="page-head"><div><p class="eyebrow">已落盘媒体</p><h1>归档文件</h1><p class="subhead">按来源聊天与月份回找已完成的归档内容。</p></div></div>
  <section class="archive-toolbar"><label class="select-wrap"><span>来源聊天</span><select v-model="selectedChat" @change="load"><option value="">全部来源</option><option v-for="chat in chats" :key="chat.id" :value="chat.id">{{ chat.title }} · {{ chat.item_count }} 个文件</option></select></label><div class="type-filters"><button v-for="type in ['', 'PHOTO', 'VIDEO', 'DOCUMENT']" :key="type" :class="{ selected: selectedType === type }" @click="selectedType = type; load()">{{ type ? mediaLabel[type] : '全部类型' }}</button></div></section>
  <section v-if="loading" class="loading-block">正在读取归档索引…</section><section v-else-if="!items.length" class="empty-state compact"><div class="empty-icon"><Search :size="26" /></div><h2>没有匹配的归档文件</h2><p>完成下载后的图片和视频会在这里生成缩略图。</p></section><section v-else class="archive-content"><div v-for="(monthItems, month) in groupedItems" :key="month" class="archive-month"><div class="section-title"><h2>{{ month }}</h2><span>{{ monthItems.length }} 个文件</span></div><div class="archive-grid"><button v-for="item in monthItems" :key="item.id" class="archive-card" @click="select(item)"><div :class="['media-preview', item.media_type.toLowerCase()]"><Image v-if="item.media_type === 'PHOTO'" :size="28" /><Video v-else-if="item.media_type === 'VIDEO'" :size="28" /><FileAudio v-else-if="item.media_type === 'AUDIO'" :size="28" /><FileText v-else :size="28" /><span v-if="item.media_type === 'VIDEO'" class="play-badge">▶</span></div><div><b>{{ item.filename }}</b><small>{{ item.chat_title }} · {{ new Date(item.message_date).toLocaleDateString('zh-CN') }}</small><span>{{ bytes(item.size_bytes) }}</span></div></button></div></div></section>
  <aside v-if="selected" class="detail-drawer"><button class="drawer-close" @click="selected = null"><X :size="20" /></button><div :class="['drawer-preview', selected.media_type.toLowerCase()]"><Image v-if="selected.media_type === 'PHOTO'" :size="40" /><Video v-else-if="selected.media_type === 'VIDEO'" :size="40" /><File v-else :size="40" /></div><span class="eyebrow">{{ mediaLabel[selected.media_type] }} · 已归档</span><h2>{{ selected.filename }}</h2><dl class="drawer-meta"><div><dt>来源聊天</dt><dd>{{ selected.chat_title }}</dd></div><div><dt>消息 ID</dt><dd>{{ selected.message_id }}</dd></div><div><dt>文件大小</dt><dd>{{ bytes(selected.size_bytes) }}</dd></div><div><dt>缩略图</dt><dd>{{ selected.thumbnail_status === 'READY' ? '已生成' : '暂不可用' }}</dd></div></dl><div class="path-box"><span>规范本地路径</span><code>{{ selected.canonical_path }}</code></div><button class="primary-button wide" @click="copyPath"><Clipboard :size="17" />{{ copied ? '路径已复制' : '复制文件路径' }}</button><p class="drawer-note"><Info :size="16" />同内容文件只保留一个物理副本，其他消息会复用此路径。</p></aside>
</template>
