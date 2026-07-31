<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Archive, CheckCircle2, FolderArchive, LayoutList, Settings, ShieldCheck, UsersRound, Wifi } from 'lucide-vue-next'
import { api, type AppState } from './api'
import { clearChatCache } from './chatCache'

const route = useRoute()
const router = useRouter()
const state = ref<AppState | null>(null)
const loading = ref(true)
const isSetup = computed(() => route.path === '/setup')

async function refreshState() {
  loading.value = true
  try {
    state.value = await api.state()
    if (!state.value.configured && !isSetup.value) await router.replace('/setup')
  } finally {
    loading.value = false
  }
}

onMounted(refreshState)
watch(state, (next) => { if (!next?.accountConnected) clearChatCache() })
</script>

<template>
  <main v-if="loading" class="boot-screen"><Archive :size="28" /><span>正在打开归档台…</span></main>
  <RouterView v-else-if="isSetup" :key="route.fullPath" @configured="refreshState" />
  <div v-else class="app-shell">
    <aside class="sidebar">
      <RouterLink to="/tasks" class="brand"><span class="brand-mark"><Archive :size="20" /></span><span>Telegram 媒体归档</span></RouterLink>
      <nav class="nav-list" aria-label="主导航">
        <RouterLink to="/tasks" class="nav-link"><LayoutList :size="19" />任务中心</RouterLink>
        <RouterLink to="/sources" class="nav-link"><UsersRound :size="19" />群组与频道</RouterLink>
        <RouterLink to="/archives" class="nav-link"><FolderArchive :size="19" />归档文件</RouterLink>
        <RouterLink to="/settings" class="nav-link"><Settings :size="19" />设置</RouterLink>
      </nav>
      <section class="connection-card">
        <div class="connection-icon"><Wifi :size="19" /></div>
        <div>
          <p><span :class="['status-dot', state?.accountConnected ? 'is-good' : state?.connectionStatus === 'invalid' ? 'is-warning' : 'is-muted']"></span>{{ state?.accountConnected ? 'Telegram 已连接' : state?.connectionStatus === 'invalid' ? '登录状态已失效' : '等待连接 Telegram' }}</p>
          <small>{{ state?.accountConnected ? state.accountName : state?.connectionStatus === 'invalid' ? '请重新连接账号' : '完成连接后即可归档' }}</small>
        </div>
      </section>
      <div v-if="state?.demoMode" class="demo-badge">演示模式 · 未连接真实 Telegram</div>
      <div class="local-note"><ShieldCheck :size="15" />数据仅存储在本机</div>
    </aside>
    <section class="page-area"><RouterView :key="route.fullPath" :app-state="state" @state-changed="refreshState" /></section>
  </div>
</template>
