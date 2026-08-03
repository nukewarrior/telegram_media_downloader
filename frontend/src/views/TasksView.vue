<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Archive, ArrowRight, Cloud, Download, FolderOpen, Gauge, Pause, Plus, Trash2, Wifi } from 'lucide-vue-next'
import TelegramLoginDialog from '../components/TelegramLoginDialog.vue'
import DeleteConfirmDialog from '../components/DeleteConfirmDialog.vue'
import DeleteResultPanel from '../components/DeleteResultPanel.vue'
import { api, type AppState, type DeleteOperation, type Task } from '../api'

const props = defineProps<{ appState: AppState | null }>()
const emit = defineEmits<{ 'state-changed': [] }>()
const tasks = ref<Task[]>([])
const loading = ref(true)
const error = ref('')
const loginOpen = ref(false)
const selectedTaskIds = ref(new Set<number>())
const confirmOpen = ref(false)
const deleting = ref(false)
const deleteOperation = ref<DeleteOperation | null>(null)

const activeStatuses = ['DOWNLOADING', 'RETRYING', 'WAITING_RATE_LIMIT', 'SCANNING', 'QUEUED']
const activeTask = computed(() => tasks.value.find((task) => activeStatuses.includes(task.status)))
const recentTasks = computed(() => tasks.value.filter((task) => task.id !== activeTask.value?.id))
const selectedCount = computed(() => selectedTaskIds.value.size)

const statusLabel: Record<string, string> = { SCANNING: '正在扫描', DOWNLOADING: '进行中', RETRYING: '等待自动重试', WAITING_RATE_LIMIT: 'Telegram 限流等待', PAUSED: '已暂停', COMPLETED: '已完成', ARCHIVE_INCOMPLETE: '归档不完整', DELETING: '删除处理中', FAILED: '失败', PARTIAL_FAILED: '部分失败', QUEUED: '等待中', CANCELLED: '已取消' }
function bytes(value: number) { return value >= 1_000_000_000 ? `${(value / 1_000_000_000).toFixed(1)} GB` : value >= 1_000_000 ? `${Math.round(value / 1_000_000)} MB` : value >= 1_000 ? `${Math.round(value / 1_000)} KB` : `${value} B` }
function percent(task: Task) { return task.total_bytes ? Math.min(100, Math.round(task.downloaded_bytes / task.total_bytes * 100)) : 0 }
function statusClass(status: string) { return `status-${status.toLowerCase()}` }
function isSelected(id: number) { return selectedTaskIds.value.has(id) }
function toggleTask(id: number) {
  if (deleting.value) return
  const next = new Set(selectedTaskIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedTaskIds.value = next
}
function clearSelection() { selectedTaskIds.value = new Set() }

async function load(showLoading = true) {
  if (showLoading) loading.value = true
  try {
    tasks.value = await api.tasks()
    const available = new Set(tasks.value.map((task) => task.id))
    selectedTaskIds.value = new Set([...selectedTaskIds.value].filter((id) => available.has(id)))
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法读取任务'
  } finally {
    if (showLoading) loading.value = false
  }
}
async function taskAction(task: Task, action: 'pause' | 'resume') { await api.action(task.id, action); await load(false) }
function requestDelete() { if (selectedCount.value) confirmOpen.value = true }
function requestSingleDelete(id: number) { selectedTaskIds.value = new Set([id]); confirmOpen.value = true }
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
  const ids = [...selectedTaskIds.value]
  if (!ids.length || deleting.value) return
  deleting.value = true
  try {
    const operation = await api.deleteTasks(ids)
    deleteOperation.value = operation
    if (operation.status === 'PENDING' || operation.status === 'RUNNING') await waitForDelete(operation)
    clearSelection()
    await load(false)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '删除任务失败'
  } finally {
    deleting.value = false
    confirmOpen.value = false
  }
}
async function retryDelete() {
  if (!deleteOperation.value || deleting.value) return
  deleting.value = true
  try {
    const operation = await api.retryDeleteOperation(deleteOperation.value.operation_id)
    deleteOperation.value = operation
    if (operation.status === 'PENDING' || operation.status === 'RUNNING') await waitForDelete(operation)
    await load(false)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '重试删除失败'
  } finally { deleting.value = false }
}
async function loginConnected() { loginOpen.value = false; emit('state-changed'); await load() }
onMounted(async () => { await load() })
</script>

<template>
  <div class="page-head"><div><p class="eyebrow">今日概览 · 2026 年 7 月 30 日</p><h1>任务中心</h1></div><RouterLink v-if="props.appState?.accountConnected" to="/tasks/new" class="primary-button"><Plus :size="18" />新建归档</RouterLink></div>
  <p v-if="error" class="form-error">{{ error }}</p>
  <section v-if="!props.appState?.accountConnected" class="empty-state">
    <div class="empty-icon"><Wifi :size="28" /></div><span class="eyebrow">{{ props.appState?.connectionStatus === 'invalid' ? '登录状态已失效' : '服务已就绪' }}</span><h2>{{ props.appState?.connectionStatus === 'invalid' ? '请重新连接 Telegram' : '连接 Telegram 后开始归档' }}</h2><p>{{ props.appState?.connectionStatus === 'invalid' ? 'Telegram 不再接受本服务的登录会话。归档文件不会受影响，重新连接即可继续使用。' : '你的 API 凭据已保存。连接账号后，即可选择已加入的聊天并安全下载媒体。' }}</p><button class="primary-button" @click="loginOpen = true"><Cloud :size="18" />{{ props.appState?.connectionStatus === 'invalid' ? '重新连接 Telegram' : '连接 Telegram' }}</button>
    <small v-if="props.appState?.demoMode">当前为演示模式，验证码输入任意六码即可完成界面体验。</small>
  </section>
  <template v-else>
    <section v-if="selectedCount" class="bulk-action-bar"><span>已选择 {{ selectedCount }} 个任务</span><button class="quiet-button" @click="clearSelection">取消选择</button><button class="danger-button" :disabled="deleting" @click="requestDelete"><Trash2 :size="16" />删除任务</button></section>
    <section v-if="activeTask" class="active-task">
      <div class="active-task-top"><label class="selection-checkbox task-selection" :class="{ selected: isSelected(activeTask.id) }"><input type="checkbox" :checked="isSelected(activeTask.id)" :aria-label="`选择任务 ${activeTask.chat_title}`" :disabled="deleting" @change="toggleTask(activeTask.id)" /><span></span></label><div class="channel-avatar">科</div><div class="active-title"><h2>{{ activeTask.chat_title }}</h2><a>{{ activeTask.chat_handle }}</a></div><div class="task-status"><span class="status-dot is-active"></span>{{ statusLabel[activeTask.status] }}</div></div>
      <div class="media-pills"><span><Archive :size="15" />图片</span><span><Download :size="15" />视频</span><span><FolderOpen :size="15" />文件</span></div>
      <div class="progress-track"><i :style="{ width: `${percent(activeTask)}%` }"></i></div><div class="progress-row"><strong>{{ percent(activeTask) }}%</strong><span>{{ activeTask.completed_count.toLocaleString() }} / {{ activeTask.total_count.toLocaleString() }} 文件</span><span>{{ bytes(activeTask.downloaded_bytes) }} / {{ bytes(activeTask.total_bytes) }}</span><span><Gauge :size="16" />{{ bytes(activeTask.speed_bytes_per_second) }}/s</span></div>
      <div class="current-file"><span>当前文件：<b>{{ activeTask.current_file }}</b></span><div><RouterLink :to="`/tasks/${activeTask.id}`" class="quiet-button">查看详情</RouterLink><button class="quiet-button" @click="taskAction(activeTask, 'pause')"><Pause :size="16" />暂停</button><button class="danger-button" :disabled="deleting" @click="requestSingleDelete(activeTask.id)"><Trash2 :size="16" />删除任务</button></div></div>
    </section>
    <section v-if="loading" class="loading-block">正在读取任务…</section>
    <section v-else class="task-section"><div class="section-title"><h2>最近任务</h2><span>{{ recentTasks.length }} 个任务</span></div><div class="task-list">
      <div v-for="task in recentTasks" :key="task.id" class="task-row" :class="{ selected: isSelected(task.id) }"><label class="selection-checkbox"><input type="checkbox" :checked="isSelected(task.id)" :aria-label="`选择任务 ${task.chat_title}`" :disabled="deleting" @change="toggleTask(task.id)" /><span></span></label><RouterLink :to="`/tasks/${task.id}`" class="task-row-link"><span :class="['task-dot', statusClass(task.status)]"></span><div class="task-main"><b>{{ task.chat_title }}</b><small>{{ task.chat_handle || 'Telegram 聊天' }}</small></div><span>{{ task.completed_count.toLocaleString() }} 文件</span><span>{{ bytes(task.total_bytes) }}</span><span :class="['status-text', statusClass(task.status)]">{{ statusLabel[task.status] }}</span><ArrowRight :size="17" /></RouterLink><button class="task-row-delete" type="button" :disabled="deleting" :aria-label="`删除任务 ${task.chat_title}`" @click="requestSingleDelete(task.id)"><Trash2 :size="17" /></button></div>
    </div></section>
  </template>
  <DeleteResultPanel :operation="deleteOperation" :busy="deleting" @retry="retryDelete" @close="deleteOperation = null" />
  <DeleteConfirmDialog :open="confirmOpen" :count="selectedCount" subject="任务" :busy="deleting" @cancel="confirmOpen = false" @confirm="confirmDelete" />
  <TelegramLoginDialog :open="loginOpen" @close="loginOpen = false" @connected="loginConnected" />
</template>
