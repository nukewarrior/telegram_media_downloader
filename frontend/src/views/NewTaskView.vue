<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft, ArrowRight, Check, FileSearch, FolderKanban, HardDrive, Image, LoaderCircle, RefreshCw, ScanSearch, Video } from 'lucide-vue-next'
import { api, type Chat, type ChatListSnapshot, type Destination } from '../api'
import { clearChatCache, readChatCache, writeChatCache } from '../chatCache'

const router = useRouter()
const step = ref(1)
const chats = ref<Chat[]>([])
const chatSnapshot = ref<ChatListSnapshot | null>(null)
const destinations = ref<Destination[]>([])
const selectedDestinationId = ref<number | null>(null)
const selectedChat = ref<Chat | null>(null)
const search = ref('')
const mediaTypes = ref<string[]>(['PHOTO', 'VIDEO', 'DOCUMENT'])
const dateStart = ref('')
const dateEnd = ref('')
const minSize = ref(0)
const maxSize = ref(0)
const scanResult = ref<{ totalCount: number; totalBytes: number; duplicateCount: number } | null>(null)
const scanning = ref(false)
const refreshingChats = ref(false)
const error = ref('')
const filteredChats = computed(() => chats.value.filter((chat) => `${chat.title} ${chat.handle ?? ''}`.toLowerCase().includes(search.value.toLowerCase())))
const payload = () => ({ chat_id: selectedChat.value!.id, chat_title: selectedChat.value!.title, chat_handle: selectedChat.value!.handle, destination_id: selectedDestinationId.value, filters: { media_types: mediaTypes.value, date_start: dateStart.value || null, date_end: dateEnd.value || null, min_size_mb: minSize.value, max_size_mb: maxSize.value } })
const formatBytes = (value: number) => value > 1_000_000_000 ? `${(value / 1_000_000_000).toFixed(1)} GB` : `${Math.round(value / 1_000_000)} MB`

function toggle(type: string) { mediaTypes.value = mediaTypes.value.includes(type) ? mediaTypes.value.filter((item) => item !== type) : [...mediaTypes.value, type] }
async function scan() { if (!selectedChat.value || !mediaTypes.value.length) return; scanning.value = true; error.value = ''; try { scanResult.value = await api.scan(payload()); step.value = 3 } catch (reason) { error.value = reason instanceof Error ? reason.message : '扫描失败' } finally { scanning.value = false } }
async function create() { try { const task = await api.createTask(payload()); await router.push(`/tasks/${task.id}`) } catch (reason) { error.value = reason instanceof Error ? reason.message : '创建任务失败' } }
function applyChatSnapshot(snapshot: ChatListSnapshot) { chats.value = snapshot.chats; chatSnapshot.value = snapshot; writeChatCache(snapshot) }
function clearUnavailableChats(reason: unknown) { if ((reason as Error & { status?: number }).status !== 409) return false; clearChatCache(); chats.value = []; chatSnapshot.value = null; selectedChat.value = null; return true }
async function loadChats() { try { applyChatSnapshot(await api.chats()) } catch (reason) { if (!clearUnavailableChats(reason) && !chats.value.length) error.value = reason instanceof Error ? reason.message : '无法读取聊天列表' } }
async function refreshChats() { refreshingChats.value = true; error.value = ''; try { applyChatSnapshot(await api.refreshChats()) } catch (reason) { const message = reason instanceof Error ? reason.message : '无法更新聊天列表'; if (!clearUnavailableChats(reason)) { error.value = message; if (chatSnapshot.value) applyChatSnapshot({ ...chatSnapshot.value, isStale: true, lastRefreshError: message }) } } finally { refreshingChats.value = false } }
function refreshedLabel() { return chatSnapshot.value?.refreshedAt ? new Date(chatSnapshot.value.refreshedAt).toLocaleString('zh-CN') : '尚未同步' }
onMounted(async () => { const cached = readChatCache(); if (cached) applyChatSnapshot(cached); try { destinations.value = (await api.destinations()).filter((item) => item.enabled); selectedDestinationId.value = destinations.value[0]?.id ?? null } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法读取归档目的地' }; await loadChats() })
</script>

<template>
  <div class="page-head wizard-head"><div><RouterLink to="/tasks" class="back-link"><ArrowLeft :size="16" />返回任务中心</RouterLink><h1>新建归档</h1></div></div>
  <ol class="wizard-steps"><li :class="{ active: step === 1, done: step > 1 }"><span>{{ step > 1 ? '✓' : '1' }}</span>选择聊天</li><li :class="{ active: step === 2, done: step > 2 }"><span>{{ step > 2 ? '✓' : '2' }}</span>筛选并扫描</li><li :class="{ active: step === 3 }"><span>3</span>确认归档</li></ol>
  <section v-if="step === 1" class="wizard-surface"><div class="wizard-copy"><span class="eyebrow">步骤 1</span><h2>选择一个来源聊天</h2><p>v1 中一个归档任务只处理一个频道或群聊，进度与来源更清晰。</p></div><div class="chat-search-row"><label class="search-input"><FileSearch :size="18" /><input v-model="search" placeholder="搜索聊天" autofocus /></label><button class="quiet-button icon-button" type="button" :disabled="refreshingChats" :aria-label="refreshingChats ? '正在刷新聊天列表' : '刷新聊天列表'" @click="refreshChats"><RefreshCw :class="{ spin: refreshingChats }" :size="17" /></button></div><p v-if="chatSnapshot?.isStale" class="chat-cache-notice">聊天列表可能不是最新的{{ chatSnapshot.lastRefreshError ? `：${chatSnapshot.lastRefreshError}` : '' }}。上次同步：{{ refreshedLabel() }}</p><p v-if="error" class="form-error" role="alert">{{ error }}</p><div class="chat-picker"><button v-for="chat in filteredChats" :key="chat.id" type="button" :class="['chat-choice', { selected: selectedChat?.id === chat.id }]" @click="selectedChat = chat"><span class="chat-avatar">{{ chat.title.slice(0, 1) }}</span><span><b>{{ chat.title }}</b><small>{{ chat.handle || (chat.type === 'GROUP' ? '群聊' : '频道') }}</small></span><Check v-if="selectedChat?.id === chat.id" :size="20" /></button></div><footer class="wizard-footer"><span>{{ selectedChat ? `已选择：${selectedChat.title}` : '请选择一个聊天' }}</span><button class="primary-button" type="button" :disabled="!selectedChat" @click="step = 2">继续<ArrowRight :size="18" /></button></footer></section>
  <section v-else-if="step === 2" class="wizard-surface filter-surface"><div class="wizard-copy"><span class="eyebrow">步骤 2</span><h2>设定归档范围</h2><p>{{ selectedChat?.title }} · {{ selectedChat?.handle }}</p></div><div class="filter-grid"><fieldset><legend>媒体类型</legend><div class="media-toggles"><button type="button" :class="{ selected: mediaTypes.includes('PHOTO') }" @click="toggle('PHOTO')"><Image :size="18" />图片</button><button type="button" :class="{ selected: mediaTypes.includes('VIDEO') }" @click="toggle('VIDEO')"><Video :size="18" />视频</button><button type="button" :class="{ selected: mediaTypes.includes('DOCUMENT') }" @click="toggle('DOCUMENT')"><FolderKanban :size="18" />文件</button></div></fieldset><fieldset><legend>时间范围</legend><div class="field-row"><label>开始日期<input v-model="dateStart" type="date" /></label><label>结束日期<input v-model="dateEnd" type="date" /></label></div></fieldset><fieldset><legend>文件大小（MB）</legend><div class="field-row"><label>最小<input v-model.number="minSize" type="number" min="0" /></label><label>最大（0 为不限）<input v-model.number="maxSize" type="number" min="0" /></label></div></fieldset></div><p v-if="error" class="form-error" role="alert">{{ error }}</p><footer class="wizard-footer"><button class="quiet-button" type="button" @click="step = 1"><ArrowLeft :size="17" />上一步</button><button class="primary-button" type="button" :disabled="scanning || !mediaTypes.length" @click="scan"><LoaderCircle v-if="scanning" class="spin" :size="18" /><ScanSearch v-else :size="18" />{{ scanning ? '正在扫描…' : '扫描匹配内容' }}</button></footer></section>
  <section v-else class="wizard-surface confirmation-surface"><div class="wizard-copy"><span class="eyebrow">步骤 3</span><h2>确认归档内容</h2><p>扫描不会下载文件。确认后任务将进入队列。</p></div><div class="scan-summary"><div><span>匹配媒体</span><strong>{{ scanResult?.totalCount.toLocaleString() }}</strong><small>个文件</small></div><div><span>预计总大小</span><strong>{{ formatBytes(scanResult?.totalBytes ?? 0) }}</strong><small>原始媒体</small></div><div><span>可复用重复项</span><strong>{{ scanResult?.duplicateCount }}</strong><small>不重复落盘</small></div></div><label class="destination-choice"><span>归档目的地</span><select v-model.number="selectedDestinationId" required><option v-for="destination in destinations" :key="destination.id" :value="destination.id">{{ destination.name }} · {{ destination.kind === 'WEBDAV' ? 'WebDAV' : '本地目录' }}</option></select></label><div class="scan-note"><HardDrive :size="20" /><span>任务创建后目的地会固定；远端目的地不可用时，任务会保留并等待重试。</span></div><p v-if="error" class="form-error" role="alert">{{ error }}</p><footer class="wizard-footer"><button class="quiet-button" type="button" @click="step = 2"><ArrowLeft :size="17" />返回修改</button><button class="primary-button" type="button" :disabled="!selectedDestinationId" @click="create">创建并开始归档<ArrowRight :size="18" /></button></footer></section>
</template>
