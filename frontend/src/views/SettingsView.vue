<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CheckCircle2, Clock3, Database, EyeOff, KeyRound, LogOut, RefreshCw, Search, ShieldAlert, Wifi } from 'lucide-vue-next'
import { api, type Settings } from '../api'
import { clearChatCache } from '../chatCache'

const settings = ref<Settings | null>(null)
const edit = ref(false)
const apiId = ref('')
const apiHash = ref('')
const concurrency = ref(3)
const archiveTimezone = ref('UTC')
const timezoneSearch = ref('')
const timezones = ref<string[]>([])
const saved = ref(false)
const concurrencySaved = ref(false)
const timezoneSaved = ref(false)
const error = ref('')
const logoutConfirm = ref(false)
const emit = defineEmits<{ 'state-changed': [] }>()
const visibleTimezones = computed(() => {
  const query = timezoneSearch.value.trim().toLowerCase()
  return timezones.value.filter((name) => name === archiveTimezone.value || !query || name.toLowerCase().includes(query))
})
async function load() {
  const [result, timezoneResult] = await Promise.all([api.settings(), api.timezones()])
  settings.value = result
  concurrency.value = result.download.maxConcurrency
  archiveTimezone.value = result.archiveTimezone ?? 'UTC'
  timezones.value = timezoneResult.timezones
}
async function save() { error.value = ''; try { await api.updateApi(apiId.value, apiHash.value); edit.value = false; saved.value = true; await load(); window.setTimeout(() => saved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveConcurrency() { error.value = ''; try { await api.updateDownloadConcurrency(concurrency.value); concurrencySaved.value = true; await load(); window.setTimeout(() => concurrencySaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveTimezone() { error.value = ''; try { await api.updateArchiveTimezone(archiveTimezone.value); timezoneSaved.value = true; emit('state-changed'); await load(); window.setTimeout(() => timezoneSaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function logout() { if (!logoutConfirm.value) { logoutConfirm.value = true; return } await api.logout(); clearChatCache(); logoutConfirm.value = false; emit('state-changed'); await load() }
onMounted(load)
</script>

<template>
  <div class="page-head"><div><p class="eyebrow">服务与安全</p><h1>设置</h1><p class="subhead">管理连接状态与服务端数据卷。敏感凭据不会回显到浏览器。</p></div></div>
  <section v-if="settings" class="settings-stack">
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Wifi :size="20" /></div><div><h2>Telegram 连接</h2><p>当前账号的授权 Session 仅保存在服务端数据卷中。</p></div></div><div class="setting-row"><div><b>{{ settings.accountConnected ? '已连接 Telegram' : settings.connectionStatus === 'invalid' ? '登录状态已失效' : '尚未连接 Telegram' }}</b><small>{{ settings.accountConnected ? `${settings.accountName || 'Telegram 账号'} · ${settings.accountPhone || ''}` : settings.connectionStatus === 'invalid' ? '归档数据仍被保留；重新连接后即可继续使用。' : '连接后可创建归档任务' }}</small></div><button v-if="settings.accountConnected" class="danger-button" @click="logout"><LogOut :size="17" />{{ logoutConfirm ? '再次点击确认退出' : '退出登录' }}</button><RouterLink v-else to="/tasks" class="primary-button">{{ settings.connectionStatus === 'invalid' ? '重新连接' : '前往连接' }}</RouterLink></div></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Clock3 :size="20" /></div><div><h2>归档时区</h2><p>新建任务会固定使用此 IANA 时区决定群组目录下的年月。</p></div></div><form class="setting-row" @submit.prevent="saveTimezone"><label>搜索时区<div class="input-with-icon"><Search :size="17" /><input v-model="timezoneSearch" placeholder="例如：Asia/Shanghai" autocomplete="off" /></div></label><label>当前时区<select v-model="archiveTimezone" required><option v-for="name in visibleTimezones" :key="name" :value="name">{{ name }}</option></select></label><div><button class="primary-button" type="submit">保存</button><small>不会移动已有文件，也不会改变已创建任务。</small></div></form><p v-if="timezoneSaved" class="success-note"><CheckCircle2 :size="17" />归档时区已保存</p></section>
    <section v-if="error" class="lan-warning"><ShieldAlert :size="21" /><div><b>设置未保存</b><p>{{ error }}</p></div></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>并发下载</h2><p>全应用共享上限；遇到 Telegram 限流或连续网络错误会自动降载并在稳定后恢复。</p></div></div><form class="setting-row" @submit.prevent="saveConcurrency"><label>同时传输文件数<input v-model.number="concurrency" type="number" min="1" max="5" required /></label><div><button class="primary-button" type="submit">保存</button><small>当前有效并发 {{ settings.download.effectiveConcurrency }} / {{ settings.download.maxConcurrency }} · 活跃 {{ settings.download.activeDownloads }}</small></div></form><p v-if="concurrencySaved" class="success-note"><CheckCircle2 :size="17" />并发设置已保存</p></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><KeyRound :size="20" /></div><div><h2>Telegram API 凭据</h2><p>API Hash 配置后仅能更新，永不回显。</p></div></div><template v-if="!edit"><div class="setting-row"><div><b>API ID：{{ settings.apiId }}</b><small>API Hash：已配置 <EyeOff :size="14" /></small></div><button class="quiet-button" @click="edit = true"><RefreshCw :size="16" />更新凭据</button></div></template><form v-else class="credentials-form" @submit.prevent="save"><label>新的 API ID<input v-model="apiId" required /></label><label>新的 API Hash<input v-model="apiHash" type="password" required /></label><div><button class="quiet-button" type="button" @click="edit = false">取消</button><button class="primary-button" type="submit">安全保存</button></div></form><p v-if="saved" class="success-note"><CheckCircle2 :size="17" />凭据已更新</p><p v-if="error" class="form-error">{{ error }}</p></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>数据卷</h2><p>下载目录由 Docker 挂载决定，应用不会列出宿主机文件系统。</p></div></div><div class="path-box"><span>DOWNLOAD_ROOT</span><code>{{ settings.downloadRoot }}</code></div></section>
    <section class="lan-warning"><ShieldAlert :size="21" /><div><b>可信局域网模式</b><p>{{ settings.trustedLanWarning }}</p></div></section>
  </section>
</template>
