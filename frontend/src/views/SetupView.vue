<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Archive, ArrowRight, CheckCircle2, Clock3, Cloud, KeyRound, LockKeyhole, Search } from 'lucide-vue-next'
import TelegramLoginDialog from '../components/TelegramLoginDialog.vue'
import { api } from '../api'

const emit = defineEmits<{ configured: [] }>()
const router = useRouter()
const step = ref<1 | 2 | 3>(1)
const apiId = ref('')
const apiHash = ref('')
const timezone = ref('UTC')
const timezoneSearch = ref('')
const timezones = ref<string[]>([])
const error = ref('')
const saving = ref(false)
const loginOpen = ref(false)
const presets = ['UTC', 'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Singapore', 'Asia/Tokyo', 'Asia/Kolkata', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'Australia/Sydney']
const visibleTimezones = computed(() => {
  const query = timezoneSearch.value.trim().toLowerCase()
  return (timezones.value.length ? timezones.value : presets).filter((name) => name === timezone.value || !query || name.toLowerCase().includes(query))
})

function browserTimezone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { return 'UTC' }
}
function chooseInitialTimezone() {
  const preferred = browserTimezone()
  timezone.value = timezones.value.includes(preferred) ? preferred : 'UTC'
}
async function completeSetup() {
  emit('configured')
  await router.replace('/tasks')
}
async function saveApi() {
  error.value = ''; saving.value = true
  try { await api.setup(apiId.value.trim(), apiHash.value.trim()); step.value = 2 } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } finally { saving.value = false }
}
async function saveTimezone() {
  error.value = ''; saving.value = true
  try { await api.updateArchiveTimezone(timezone.value); step.value = 3 } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } finally { saving.value = false }
}
async function loginConnected() { loginOpen.value = false; await completeSetup() }
onMounted(async () => {
  try {
    const state = await api.state()
    if (state.configured) { await router.replace('/tasks'); return }
    if (state.apiConfigured) step.value = 2
    try { timezones.value = (await api.timezones()).timezones; chooseInitialTimezone() } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法读取时区列表' }
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法读取配置状态' }
})
</script>

<template>
  <main class="setup-shell">
    <section class="setup-aside">
      <div class="brand"><span class="brand-mark"><Archive :size="20" /></span><span>Telegram 媒体归档</span></div>
      <div class="setup-copy"><span class="eyebrow">首次配置</span><h1>{{ step === 1 ? '先配置你的\nTelegram 开发凭据。' : step === 2 ? '选择归档文件\n使用的时区。' : '连接 Telegram，\n或稍后再说。' }}</h1><p>时区决定新建归档任务按哪一个本地年月存放文件；之后可在设置中调整。</p></div>
      <ol class="setup-steps"><li :class="{ active: step === 1, completed: step > 1 }"><CheckCircle2 v-if="step > 1" :size="18" /><span v-else>1</span><span>配置 API 凭据</span></li><li :class="{ active: step === 2, completed: step > 2 }"><CheckCircle2 v-if="step > 2" :size="18" /><span v-else>2</span><span>设置归档时区</span></li><li :class="{ active: step === 3 }"><span>3</span><span>连接 Telegram（可跳过）</span></li></ol>
    </section>
    <section class="setup-main">
      <form v-if="step === 1" class="setup-form" @submit.prevent="saveApi">
        <div><span class="eyebrow">步骤 1 / 3</span><h2>配置 Telegram API</h2><p>在 <a href="https://my.telegram.org" target="_blank" rel="noreferrer">my.telegram.org</a> 创建应用后填写凭据。</p></div>
        <label>API ID<input v-model="apiId" inputmode="numeric" autocomplete="off" placeholder="例如：12345678" required /></label>
        <label>API Hash<div class="input-with-icon"><KeyRound :size="18" /><input v-model="apiHash" type="password" autocomplete="off" placeholder="32 位 API Hash" required /></div></label>
        <div class="security-callout"><LockKeyhole :size="18" /><span>API Hash、验证码和两步验证密码不会回显或写入应用日志。此服务未启用访问认证，仅可用于可信局域网，绝不可暴露公网。</span></div>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button class="primary-button wide" :disabled="saving">{{ saving ? '正在保存…' : '保存并继续' }}<ArrowRight :size="18" /></button>
      </form>
      <form v-else-if="step === 2" class="setup-form" @submit.prevent="saveTimezone">
        <div><span class="eyebrow">步骤 2 / 3</span><h2>设置归档时区</h2><p>已预选当前浏览器的时区。它会在创建任务时固定下来，保证同一任务的目录一致。</p></div>
        <div class="timezone-presets"><button v-for="preset in presets" :key="preset" type="button" :class="{ selected: timezone === preset }" @click="timezone = preset">{{ preset }}</button></div>
        <label>搜索或输入 IANA 时区<div class="input-with-icon"><Search :size="18" /><input v-model="timezoneSearch" autocomplete="off" placeholder="例如：Asia/Shanghai" /></div></label>
        <label>归档时区<select v-model="timezone" required><option v-for="name in visibleTimezones" :key="name" :value="name">{{ name }}</option></select></label>
        <div class="security-callout"><Clock3 :size="18" /><span>修改设置不会移动已有文件，也不会改变已创建任务的归档目录规则。</span></div>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button class="primary-button wide" :disabled="saving">{{ saving ? '正在保存…' : '确认时区并继续' }}<ArrowRight :size="18" /></button>
      </form>
      <section v-else class="setup-form"><div><span class="eyebrow">步骤 3 / 3</span><h2>连接 Telegram</h2><p>连接后即可选择聊天并创建归档任务。你也可以先进入任务中心，之后再连接。</p></div><div class="security-callout"><Cloud :size="18" /><span>验证码和两步验证密码仅用于本次连接，不会保存在服务中。</span></div><button class="primary-button wide" @click="loginOpen = true">连接 Telegram<ArrowRight :size="18" /></button><button class="text-button" type="button" @click="completeSetup">稍后登录，进入任务中心</button></section>
    </section>
  </main>
  <TelegramLoginDialog :open="loginOpen" @close="loginOpen = false" @connected="loginConnected" />
</template>
