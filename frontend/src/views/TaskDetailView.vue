<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { AlertTriangle, ArrowLeft, CirclePause, Clock3, FileWarning, Gauge, Play, RefreshCw, XCircle } from 'lucide-vue-next'
import { api, type Task } from '../api'

const route = useRoute()
const task = ref<Task | null>(null)
const error = ref('')
const confirmCancel = ref(false)
let source: EventSource | null = null
const taskId = Number(route.params.id)
const bytes = (value: number) => value >= 1_000_000_000 ? `${(value / 1_000_000_000).toFixed(1)} GB` : `${Math.max(1, Math.round(value / 1_000_000))} MB`
const percent = () => task.value?.total_bytes ? Math.min(100, Math.round(task.value.downloaded_bytes / task.value.total_bytes * 100)) : 0
const statusLabel: Record<string, string> = { DOWNLOADING: '正在下载', PAUSED: '已暂停', COMPLETED: '已完成', FAILED: '失败', PARTIAL_FAILED: '部分失败', QUEUED: '等待中', CANCELLED: '已取消' }
async function load() { try { task.value = await api.task(taskId) } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法读取任务' } }
async function action(kind: 'pause' | 'resume' | 'retry' | 'cancel') { if (kind === 'cancel' && !confirmCancel.value) { confirmCancel.value = true; return } task.value = await api.action(taskId, kind); confirmCancel.value = false }
onMounted(async () => { await load(); source = new EventSource(api.eventsUrl(taskId)); source.addEventListener('task', (event) => { task.value = JSON.parse((event as MessageEvent).data) }) })
onBeforeUnmount(() => source?.close())
</script>

<template>
  <div class="page-head"><div><RouterLink to="/tasks" class="back-link"><ArrowLeft :size="16" />返回任务中心</RouterLink><h1>{{ task?.chat_title || '任务详情' }}</h1><p class="subhead">{{ task?.chat_handle }} · 单聊天归档任务</p></div><span v-if="task" :class="['large-status', `status-${task.status.toLowerCase()}`]">{{ statusLabel[task.status] }}</span></div>
  <p v-if="error" class="form-error">{{ error }}</p>
  <template v-else-if="task"><section class="detail-progress"><div class="detail-progress-title"><div><span class="eyebrow">总体进度</span><strong>{{ percent() }}%</strong></div><div class="detail-controls"><button v-if="task.status === 'DOWNLOADING'" class="quiet-button" @click="action('pause')"><CirclePause :size="17" />暂停</button><button v-if="task.status === 'PAUSED' || task.status === 'QUEUED'" class="primary-button" @click="action('resume')"><Play :size="17" />继续下载</button><button v-if="task.status === 'FAILED' || task.status === 'PARTIAL_FAILED'" class="primary-button" @click="action('retry')"><RefreshCw :size="17" />失败重试</button><button class="danger-button" @click="action('cancel')"><XCircle :size="17" />{{ confirmCancel ? '再次点击确认取消' : '取消任务' }}</button></div></div><div class="progress-track large"><i :style="{ width: `${percent()}%` }"></i></div><div class="metric-row"><div><span>已完成</span><b>{{ task.completed_count.toLocaleString() }} / {{ task.total_count.toLocaleString() }} 文件</b></div><div><span>已下载</span><b>{{ bytes(task.downloaded_bytes) }} / {{ bytes(task.total_bytes) }}</b></div><div><span>下载速度</span><b><Gauge :size="16" />{{ bytes(task.speed_bytes_per_second) }}/s</b></div><div><span>预计剩余</span><b><Clock3 :size="16" />{{ task.status === 'DOWNLOADING' ? '约 14 分钟' : '—' }}</b></div></div></section><div class="detail-grid"><section class="detail-panel"><h2>当前文件</h2><p class="current-filename">{{ task.current_file || '当前没有传输中的文件' }}</p><div class="log-list"><p>任务创建 · {{ new Date(task.created_at).toLocaleString('zh-CN') }}</p><p v-if="task.status === 'PAUSED'">任务已安全暂停，恢复时将从 .part 文件继续。</p><p v-else-if="task.status === 'DOWNLOADING'">正在写入临时文件，完成后将校验大小和内容哈希。</p><p v-else>任务状态：{{ statusLabel[task.status] }}</p></div></section><section class="detail-panel"><h2>归档筛选</h2><dl class="filter-summary"><div><dt>媒体类型</dt><dd>{{ Array.isArray(task.filters.media_types) ? task.filters.media_types.join(' · ') : '全部' }}</dd></div><div><dt>时间范围</dt><dd>{{ task.filters.date_start || '不限' }} 至 {{ task.filters.date_end || '不限' }}</dd></div><div><dt>文件大小</dt><dd>{{ task.filters.min_size_mb || 0 }} MB 至 {{ task.filters.max_size_mb || '不限' }} MB</dd></div></dl></section></div><section v-if="task.failed_count" class="failed-panel"><FileWarning :size="20" /><div><h2>有 {{ task.failed_count }} 个文件需要处理</h2><p>网络错误会自动重试；仍失败的文件可从此任务再次加入队列。</p></div><button class="quiet-button" @click="action('retry')">重试失败项</button></section></template>
</template>
