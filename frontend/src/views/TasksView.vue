<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Archive, ArrowRight, CheckCircle2, Cloud, Download, FolderOpen, Gauge, Pause, Plus, Wifi } from 'lucide-vue-next'
import { api, type AppState, type Task } from '../api'

const props = defineProps<{ appState: AppState | null }>()
const emit = defineEmits<{ 'state-changed': [] }>()
const router = useRouter()
const tasks = ref<Task[]>([])
const loading = ref(true)
const loginStep = ref<'closed' | 'phone' | 'code' | 'password'>('closed')
const phone = ref('')
const code = ref('')
const password = ref('')
const attemptId = ref('')
const loginHint = ref('')
const error = ref('')
const loginBusy = ref(false)

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
function closeLogin() {
  loginStep.value = 'closed'
  code.value = ''
  password.value = ''
  error.value = ''
  loginHint.value = ''
}
async function sendCode() {
  error.value = ''; loginBusy.value = true
  try { const result = await api.sendCode(phone.value); attemptId.value = result.attemptId; loginHint.value = result.demoHint ?? ''; loginStep.value = result.passwordRequired ? 'password' : 'code' } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法发送验证码' } finally { loginBusy.value = false }
}
async function verifyCode() {
  error.value = ''; loginBusy.value = true
  try {
    const result = await api.verifyCode(attemptId.value, code.value)
    if (result.passwordRequired) { loginStep.value = 'password'; password.value = ''; return }
    closeLogin(); emit('state-changed'); await load()
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '验证码无效' } finally { loginBusy.value = false }
}
async function verifyPassword() {
  error.value = ''; loginBusy.value = true
  try { await api.verifyPassword(attemptId.value, password.value); closeLogin(); emit('state-changed'); await load() } catch (reason) { error.value = reason instanceof Error ? reason.message : '密码无效' } finally { loginBusy.value = false }
}
onMounted(async () => {
  await load()
})
</script>

<template>
  <div class="page-head"><div><p class="eyebrow">今日概览 · 2026 年 7 月 30 日</p><h1>任务中心</h1></div><RouterLink v-if="props.appState?.accountConnected" to="/tasks/new" class="primary-button"><Plus :size="18" />新建归档</RouterLink></div>
  <section v-if="!props.appState?.accountConnected" class="empty-state">
    <div class="empty-icon"><Wifi :size="28" /></div><span class="eyebrow">{{ props.appState?.connectionStatus === 'invalid' ? '登录状态已失效' : '服务已就绪' }}</span><h2>{{ props.appState?.connectionStatus === 'invalid' ? '请重新连接 Telegram' : '连接 Telegram 后开始归档' }}</h2><p>{{ props.appState?.connectionStatus === 'invalid' ? 'Telegram 不再接受本服务的登录会话。归档文件不会受影响，重新连接即可继续使用。' : '你的 API 凭据已保存。连接账号后，即可选择已加入的聊天并安全下载媒体。' }}</p><button class="primary-button" @click="loginStep = 'phone'"><Cloud :size="18" />{{ props.appState?.connectionStatus === 'invalid' ? '重新连接 Telegram' : '连接 Telegram' }}</button>
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
  <div v-if="loginStep !== 'closed'" class="dialog-backdrop"><section class="login-dialog" role="dialog" aria-modal="true" aria-labelledby="telegram-login-title"><button class="dialog-close" aria-label="关闭" @click="closeLogin">×</button><div class="dialog-icon"><Wifi :size="22" /></div><template v-if="loginStep === 'phone'"><h2 id="telegram-login-title">连接 Telegram</h2><p>输入带国家区号的手机号。验证码仅用于本次登录，不会保存在本服务中。</p><label>手机号<input v-model="phone" placeholder="+86 138 0000 0000" autocomplete="tel" :disabled="loginBusy" autofocus /></label><button class="primary-button wide" :disabled="loginBusy" @click="sendCode">{{ loginBusy ? '正在发送…' : '发送验证码' }}<ArrowRight v-if="!loginBusy" :size="18" /></button></template><template v-else-if="loginStep === 'code'"><h2 id="telegram-login-title">输入验证码</h2><p>验证码已发送到 {{ phone }}。服务重启或超过十分钟后，请重新发送。</p><label>验证码<input v-model="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="123456" :disabled="loginBusy" autofocus /></label><small v-if="loginHint" class="tip">{{ loginHint }}</small><button class="primary-button wide" :disabled="loginBusy" @click="verifyCode">{{ loginBusy ? '正在验证…' : '验证并连接' }}<CheckCircle2 v-if="!loginBusy" :size="18" /></button><button class="text-button" type="button" :disabled="loginBusy" @click="sendCode">重新发送验证码</button></template><template v-else><h2 id="telegram-login-title">两步验证</h2><p>此账号启用了额外密码保护。密码仅用于本次验证。</p><label>两步验证密码<input v-model="password" type="password" autocomplete="current-password" :disabled="loginBusy" autofocus /></label><button class="primary-button wide" :disabled="loginBusy" @click="verifyPassword">{{ loginBusy ? '正在验证…' : '完成连接' }}<CheckCircle2 v-if="!loginBusy" :size="18" /></button></template><p v-if="error" class="form-error">{{ error }}</p></section></div>
</template>
