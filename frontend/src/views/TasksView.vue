<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Archive, ArrowRight, Cloud, Download, FolderOpen, Gauge, Pause, Plus, Wifi } from 'lucide-vue-next'
import TelegramLoginDialog from '../components/TelegramLoginDialog.vue'
import { api, type AppState, type Task } from '../api'

const props = defineProps<{ appState: AppState | null }>()
const emit = defineEmits<{ 'state-changed': [] }>()
const tasks = ref<Task[]>([])
const loading = ref(true)
const loginOpen = ref(false)

const activeTask = computed(() => tasks.value.find((task) => ['DOWNLOADING', 'RETRYING', 'WAITING_RATE_LIMIT'].includes(task.status)))
const recentTasks = computed(() => tasks.value.filter((task) => !['DOWNLOADING', 'RETRYING', 'WAITING_RATE_LIMIT'].includes(task.status)))

const statusLabel: Record<string, string> = { SCANNING: '正在扫描', DOWNLOADING: '进行中', RETRYING: '等待自动重试', WAITING_RATE_LIMIT: 'Telegram 限流等待', PAUSED: '已暂停', COMPLETED: '已完成', FAILED: '失败', PARTIAL_FAILED: '部分失败', QUEUED: '等待中', CANCELLED: '已取消' }
function bytes(value: number) { return value >= 1_000_000_000 ? `${(value / 1_000_000_000).toFixed(1)} GB` : value >= 1_000_000 ? `${Math.round(value / 1_000_000)} MB` : value >= 1_000 ? `${Math.round(value / 1_000)} KB` : `${value} B` }
function percent(task: Task) { return task.total_bytes ? Math.min(100, Math.round(task.downloaded_bytes / task.total_bytes * 100)) : 0 }
function statusClass(status: string) { return `status-${status.toLowerCase()}` }

async function load(showLoading = true) {
  if (showLoading) loading.value = true
  try { tasks.value = await api.tasks() } finally { if (showLoading) loading.value = false }
}
async function taskAction(task: Task, action: 'pause' | 'resume') { await api.action(task.id, action); await load() }
async function loginConnected() { loginOpen.value = false; emit('state-changed'); await load() }
onMounted(async () => {
  await load()
})
</script>

<template>
  <div class="page-head"><div><p class="eyebrow">今日概览 · 2026 年 7 月 30 日</p><h1>任务中心</h1></div><RouterLink v-if="props.appState?.accountConnected" to="/tasks/new" class="primary-button"><Plus :size="18" />新建归档</RouterLink></div>
  <section v-if="!props.appState?.accountConnected" class="empty-state">
    <div class="empty-icon"><Wifi :size="28" /></div><span class="eyebrow">{{ props.appState?.connectionStatus === 'invalid' ? '登录状态已失效' : '服务已就绪' }}</span><h2>{{ props.appState?.connectionStatus === 'invalid' ? '请重新连接 Telegram' : '连接 Telegram 后开始归档' }}</h2><p>{{ props.appState?.connectionStatus === 'invalid' ? 'Telegram 不再接受本服务的登录会话。归档文件不会受影响，重新连接即可继续使用。' : '你的 API 凭据已保存。连接账号后，即可选择已加入的聊天并安全下载媒体。' }}</p><button class="primary-button" @click="loginOpen = true"><Cloud :size="18" />{{ props.appState?.connectionStatus === 'invalid' ? '重新连接 Telegram' : '连接 Telegram' }}</button>
    <small v-if="props.appState?.demoMode">当前为演示模式，验证码输入任意六码即可完成界面体验。</small>
  </section>
  <template v-else>
    <section v-if="activeTask" class="active-task">
      <div class="active-task-top"><div class="channel-avatar">科</div><div class="active-title"><h2>{{ activeTask.chat_title }}</h2><a>{{ activeTask.chat_handle }}</a></div><div class="task-status"><span class="status-dot is-active"></span>{{ statusLabel[activeTask.status] }}</div></div>
      <div class="media-pills"><span><Archive :size="15" />图片</span><span><Download :size="15" />视频</span><span><FolderOpen :size="15" />文件</span></div>
      <div class="progress-track"><i :style="{ width: `${percent(activeTask)}%` }"></i></div>
      <div class="progress-row"><strong>{{ percent(activeTask) }}%</strong><span>{{ activeTask.completed_count.toLocaleString() }} / {{ activeTask.total_count.toLocaleString() }} 文件</span><span>{{ bytes(activeTask.downloaded_bytes) }} / {{ bytes(activeTask.total_bytes) }}</span><span><Gauge :size="16" />{{ bytes(activeTask.speed_bytes_per_second) }}/s</span></div>
      <div class="current-file"><span>当前文件：<b>{{ activeTask.current_file }}</b></span><div><RouterLink :to="`/tasks/${activeTask.id}`" class="quiet-button">查看详情</RouterLink><button class="quiet-button" @click="taskAction(activeTask, 'pause')"><Pause :size="16" />暂停</button></div></div>
    </section>
    <section v-if="loading" class="loading-block">正在读取任务…</section>
    <section v-else class="task-section"><div class="section-title"><h2>最近任务</h2><span>{{ recentTasks.length }} 个任务</span></div><div class="task-list"><RouterLink v-for="task in recentTasks" :key="task.id" :to="`/tasks/${task.id}`" class="task-row"><span :class="['task-dot', statusClass(task.status)]"></span><div class="task-main"><b>{{ task.chat_title }}</b><small>{{ task.chat_handle || 'Telegram 聊天' }}</small></div><span>{{ task.completed_count.toLocaleString() }} 文件</span><span>{{ bytes(task.total_bytes) }}</span><span :class="['status-text', statusClass(task.status)]">{{ statusLabel[task.status] }}</span><ArrowRight :size="17" /></RouterLink></div></section>
  </template>
  <TelegramLoginDialog :open="loginOpen" @close="loginOpen = false" @connected="loginConnected" />
</template>
