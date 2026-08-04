<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CheckCircle2, Clock3, Database, EyeOff, KeyRound, LogOut, Plus, RefreshCw, Search, ShieldAlert, Wifi, X } from 'lucide-vue-next'
import { api, type Destination, type Settings } from '../api'
import { clearChatCache } from '../chatCache'

const settings = ref<Settings | null>(null)
const edit = ref(false)
const apiId = ref('')
const apiHash = ref('')
const concurrency = ref(3)
const sourceCacheMiB = ref(2048)
const archiveTimezone = ref('UTC')
const timezoneSearch = ref('')
const timezones = ref<string[]>([])
const saved = ref(false)
const concurrencySaved = ref(false)
const sourceCacheSaved = ref(false)
const timezoneSaved = ref(false)
const localDestinationName = ref('')
const localDestinationRoot = ref('')
const localDestinationSaving = ref(false)
const localDestinationTesting = ref(false)
const localDestinationMessage = ref('')
const localDestinationMessageKind = ref<'success' | 'error' | ''>('')
const destinationDialogOpen = ref(false)
const destinationDialog = ref<HTMLElement | null>(null)
const destinationDialogFirstField = ref<HTMLInputElement | null>(null)
const destinationDialogTrigger = ref<HTMLButtonElement | null>(null)
const newWebdavButton = ref<HTMLButtonElement | null>(null)
const destinationName = ref('')
const destinationWebdavUrl = ref('')
const destinationWebdavUsername = ref('')
const destinationWebdavPassword = ref('')
const destinationRemoteRoot = ref('telegram-archive')
const destinationSaving = ref(false)
const destinationTesting = ref(false)
const testingDestinationId = ref<number | null>(null)
const destinationSaved = ref(false)
const destinationSavedMessage = ref('')
const destinationMessage = ref('')
const destinationMessageKind = ref<'success' | 'error' | ''>('')
const editingDestinationId = ref<number | null>(null)
const editingDestinationEnabled = ref(true)
const destinationTestedFingerprint = ref<string | null>(null)
const destinationTestResults = ref<Record<number, { ok: boolean; message: string }>>({})
const error = ref('')
const logoutConfirm = ref(false)
const emit = defineEmits<{ 'state-changed': [] }>()
const editingDestination = computed(() => settings.value?.destinations.find((item) => item.id === editingDestinationId.value) ?? null)
const destinationDialogTitle = computed(() => editingDestinationId.value === null ? '添加 WebDAV 连接' : '编辑 WebDAV 连接')
const destinationCanSave = computed(() => destinationTestedFingerprint.value === destinationFingerprint())
const visibleTimezones = computed(() => {
  const query = timezoneSearch.value.trim().toLowerCase()
  return timezones.value.filter((name) => name === archiveTimezone.value || !query || name.toLowerCase().includes(query))
})
watch([destinationName, destinationWebdavUrl, destinationWebdavUsername, destinationWebdavPassword, destinationRemoteRoot, editingDestinationEnabled], () => {
  if (destinationTestedFingerprint.value === null) return
  destinationTestedFingerprint.value = null
  if (destinationMessageKind.value === 'success') {
    destinationMessage.value = ''
    destinationMessageKind.value = ''
  }
})
watch(destinationDialogOpen, (open) => {
  document.body.classList.toggle('dialog-open', open)
  if (open) {
    void nextTick(() => destinationDialogFirstField.value?.focus())
  }
})
async function load() {
  const [result, timezoneResult] = await Promise.all([api.settings(), api.timezones()])
  settings.value = result
  concurrency.value = result.download.maxConcurrency
  sourceCacheMiB.value = Math.round(result.sourceCache.maxBytes / (1024 * 1024))
  archiveTimezone.value = result.archiveTimezone ?? 'UTC'
  timezones.value = timezoneResult.timezones
}
async function save() { error.value = ''; try { await api.updateApi(apiId.value, apiHash.value); edit.value = false; saved.value = true; await load(); window.setTimeout(() => saved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveConcurrency() { error.value = ''; try { await api.updateDownloadConcurrency(concurrency.value); concurrencySaved.value = true; await load(); window.setTimeout(() => concurrencySaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveSourceCache() { error.value = ''; try { await api.updateSourceCache(sourceCacheMiB.value * 1024 * 1024); sourceCacheSaved.value = true; await load(); window.setTimeout(() => sourceCacheSaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveTimezone() { error.value = ''; try { await api.updateArchiveTimezone(archiveTimezone.value); timezoneSaved.value = true; emit('state-changed'); await load(); window.setTimeout(() => timezoneSaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
function destinationPayload() {
  return {
    name: destinationName.value,
    kind: 'WEBDAV' as const,
    local_root: null,
    webdav_url: destinationWebdavUrl.value,
    webdav_username: destinationWebdavUsername.value || null,
    webdav_password: destinationWebdavPassword.value || null,
    remote_root: destinationRemoteRoot.value,
    enabled: editingDestinationId.value === null ? true : editingDestinationEnabled.value,
  }
}
function destinationFingerprint(payload = destinationPayload()) { return JSON.stringify(payload) }
function clearDestinationForm() {
  editingDestinationId.value = null
  editingDestinationEnabled.value = true
  destinationName.value = ''
  destinationWebdavUrl.value = ''
  destinationWebdavUsername.value = ''
  destinationWebdavPassword.value = ''
  destinationRemoteRoot.value = 'telegram-archive'
  destinationTestedFingerprint.value = null
  destinationMessage.value = ''
  destinationMessageKind.value = ''
}
function openNewWebdav() {
  destinationDialogTrigger.value = newWebdavButton.value
  clearDestinationForm()
  destinationSaved.value = false
  destinationSavedMessage.value = ''
  destinationDialogOpen.value = true
}
function startDestinationEdit(destination: Destination, trigger: EventTarget | null) {
  if (destination.is_system || destination.kind !== 'WEBDAV') return
  destinationDialogTrigger.value = trigger instanceof HTMLButtonElement ? trigger : null
  editingDestinationId.value = destination.id
  editingDestinationEnabled.value = destination.enabled
  destinationName.value = destination.name
  destinationWebdavUrl.value = destination.webdav_url ?? ''
  destinationWebdavUsername.value = destination.webdav_username ?? ''
  destinationWebdavPassword.value = ''
  destinationRemoteRoot.value = destination.remote_root
  destinationTestedFingerprint.value = null
  destinationMessage.value = ''
  destinationMessageKind.value = ''
  destinationSaved.value = false
  destinationSavedMessage.value = ''
  destinationDialogOpen.value = true
}
function closeDestinationDialog() {
  if (destinationTesting.value || destinationSaving.value) return
  destinationDialogOpen.value = false
  clearDestinationForm()
  void nextTick(() => destinationDialogTrigger.value?.focus())
}
function handleDestinationDialogKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeDestinationDialog()
    return
  }
  if (event.key !== 'Tab' || !destinationDialog.value) return
  const focusable = Array.from(destinationDialog.value.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter((element) => !element.hasAttribute('hidden'))
  if (!focusable.length) {
    event.preventDefault()
    destinationDialog.value.focus()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
async function testDestination() {
  destinationTesting.value = true
  destinationMessage.value = ''
  destinationMessageKind.value = ''
  const payload = destinationPayload()
  try {
    const result = editingDestinationId.value === null
      ? await api.testDestination(payload)
      : await api.testSavedDestination(editingDestinationId.value, payload)
    destinationTestedFingerprint.value = destinationFingerprint(payload)
    destinationMessage.value = result.message
    destinationMessageKind.value = 'success'
  } catch (reason) {
    destinationTestedFingerprint.value = null
    destinationMessage.value = reason instanceof Error ? reason.message : '连接测试失败'
    destinationMessageKind.value = 'error'
  } finally {
    destinationTesting.value = false
  }
}
async function saveDestination() {
  if (!destinationCanSave.value) {
    destinationMessage.value = '请先测试当前 WebDAV 配置，测试通过后才能保存。'
    destinationMessageKind.value = 'error'
    return
  }
  destinationSaving.value = true
  destinationMessage.value = ''
  destinationMessageKind.value = ''
  const editingId = editingDestinationId.value
  try {
    if (editingId === null) await api.createDestination(destinationPayload())
    else await api.updateDestination(editingId, destinationPayload())
    destinationSaved.value = true
    destinationSavedMessage.value = editingId === null ? '目的地已添加' : '目的地已更新'
    destinationDialogOpen.value = false
    clearDestinationForm()
    void nextTick(() => destinationDialogTrigger.value?.focus())
    await load()
    window.setTimeout(() => { destinationSaved.value = false }, 2500)
  } catch (reason) {
    destinationMessage.value = reason instanceof Error ? reason.message : '保存失败'
    destinationMessageKind.value = 'error'
  } finally {
    destinationSaving.value = false
  }
}
function localDestinationPayload() {
  return {
    name: localDestinationName.value,
    kind: 'LOCAL' as const,
    local_root: localDestinationRoot.value,
    webdav_url: null,
    webdav_username: null,
    webdav_password: null,
    remote_root: '',
    enabled: true,
  }
}
async function testLocalDestination() {
  localDestinationTesting.value = true
  localDestinationMessage.value = ''
  localDestinationMessageKind.value = ''
  try {
    localDestinationMessage.value = (await api.testDestination(localDestinationPayload())).message
    localDestinationMessageKind.value = 'success'
  } catch (reason) {
    localDestinationMessage.value = reason instanceof Error ? reason.message : '连接测试失败'
    localDestinationMessageKind.value = 'error'
  } finally {
    localDestinationTesting.value = false
  }
}
async function saveLocalDestination() {
  localDestinationSaving.value = true
  localDestinationMessage.value = ''
  localDestinationMessageKind.value = ''
  try {
    await api.createDestination(localDestinationPayload())
    destinationSaved.value = true
    destinationSavedMessage.value = '本地目的地已添加'
    localDestinationName.value = ''
    localDestinationRoot.value = ''
    await load()
    window.setTimeout(() => { destinationSaved.value = false }, 2500)
  } catch (reason) {
    localDestinationMessage.value = reason instanceof Error ? reason.message : '保存失败'
    localDestinationMessageKind.value = 'error'
  } finally {
    localDestinationSaving.value = false
  }
}
async function testSavedDestinationRow(id: number) {
  testingDestinationId.value = id
  const results = { ...destinationTestResults.value }
  delete results[id]
  destinationTestResults.value = results
  try {
    const result = await api.testSavedDestination(id)
    destinationTestResults.value = { ...destinationTestResults.value, [id]: { ok: true, message: result.message } }
  } catch (reason) {
    destinationTestResults.value = { ...destinationTestResults.value, [id]: { ok: false, message: reason instanceof Error ? reason.message : '连接测试失败' } }
  } finally {
    testingDestinationId.value = null
  }
}
async function disableDestination(id: number) { destinationMessage.value = ''; try { await api.disableDestination(id); await load() } catch (reason) { destinationMessage.value = reason instanceof Error ? reason.message : '停用失败'; destinationMessageKind.value = 'error' } }
async function enableDestination(id: number) { destinationMessage.value = ''; try { await api.enableDestination(id); await load() } catch (reason) { destinationMessage.value = reason instanceof Error ? reason.message : '启用失败'; destinationMessageKind.value = 'error' } }
async function logout() { if (!logoutConfirm.value) { logoutConfirm.value = true; return } await api.logout(); clearChatCache(); logoutConfirm.value = false; emit('state-changed'); await load() }
onMounted(load)
onBeforeUnmount(() => document.body.classList.remove('dialog-open'))
</script>

<template>
  <h1 class="sr-only">设置</h1>
  <section v-if="settings" class="settings-stack">
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Wifi :size="20" /></div><div><h2>Telegram 连接</h2><p>当前账号的授权 Session 仅保存在服务端数据卷中。</p></div></div><div class="setting-row"><div><b>{{ settings.accountConnected ? '已连接 Telegram' : settings.connectionStatus === 'invalid' ? '登录状态已失效' : '尚未连接 Telegram' }}</b><small>{{ settings.accountConnected ? `${settings.accountName || 'Telegram 账号'} · ${settings.accountPhone || ''}` : settings.connectionStatus === 'invalid' ? '归档数据仍被保留；重新连接后即可继续使用。' : '连接后可创建归档任务' }}</small></div><button v-if="settings.accountConnected" class="danger-button" @click="logout"><LogOut :size="17" />{{ logoutConfirm ? '再次点击确认退出' : '退出登录' }}</button><RouterLink v-else to="/tasks" class="primary-button">{{ settings.connectionStatus === 'invalid' ? '重新连接' : '前往连接' }}</RouterLink></div></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Clock3 :size="20" /></div><div><h2>归档时区</h2><p>新建任务会固定使用此 IANA 时区决定群组目录下的年月。</p></div></div><form class="setting-row" @submit.prevent="saveTimezone"><label>搜索时区<div class="input-with-icon"><Search :size="17" /><input v-model="timezoneSearch" placeholder="例如：Asia/Shanghai" autocomplete="off" /></div></label><label>当前时区<select v-model="archiveTimezone" required><option v-for="name in visibleTimezones" :key="name" :value="name">{{ name }}</option></select></label><div><button class="primary-button" type="submit">保存</button><small>不会移动已有文件，也不会改变已创建任务。</small></div></form><p v-if="timezoneSaved" class="success-note"><CheckCircle2 :size="17" />归档时区已保存</p></section>
    <section v-if="error" class="lan-warning"><ShieldAlert :size="21" /><div><b>设置未保存</b><p>{{ error }}</p></div></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>并发下载</h2><p>全应用共享上限；遇到 Telegram 限流或连续网络错误会自动降载并在稳定后恢复。</p></div></div><form class="setting-row" @submit.prevent="saveConcurrency"><label>同时传输文件数<input v-model.number="concurrency" type="number" min="1" max="5" required /></label><div><button class="primary-button" type="submit">保存</button><small>当前有效并发 {{ settings.download.effectiveConcurrency }} / {{ settings.download.maxConcurrency }} · 活跃 {{ settings.download.activeDownloads }}</small></div></form><p v-if="concurrencySaved" class="success-note"><CheckCircle2 :size="17" />并发设置已保存</p></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>来源浏览缓存</h2><p>已浏览媒体的索引会长期保留；超出上限时自动淘汰最久未访问的图片和视频缩略图。</p></div></div><form class="setting-row" @submit.prevent="saveSourceCache"><label>缩略图缓存上限（MiB）<input v-model.number="sourceCacheMiB" type="number" min="256" max="20480" required /></label><div><button class="primary-button" type="submit">保存</button><small>默认 2048 MiB；不会下载完整来源文件。</small></div></form><p v-if="sourceCacheSaved" class="success-note"><CheckCircle2 :size="17" />来源缓存上限已保存</p></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><KeyRound :size="20" /></div><div><h2>Telegram API 凭据</h2><p>API Hash 配置后仅能更新，永不回显。</p></div></div><template v-if="!edit"><div class="setting-row"><div><b>API ID：{{ settings.apiId }}</b><small>API Hash：已配置 <EyeOff :size="14" /></small></div><button class="quiet-button" @click="edit = true"><RefreshCw :size="16" />更新凭据</button></div></template><form v-else class="credentials-form" @submit.prevent="save"><label>新的 API ID<input v-model="apiId" required /></label><label>新的 API Hash<input v-model="apiHash" type="password" required /></label><div><button class="quiet-button" type="button" @click="edit = false">取消</button><button class="primary-button" type="submit">安全保存</button></div></form><p v-if="saved" class="success-note"><CheckCircle2 :size="17" />凭据已更新</p><p v-if="error" class="form-error">{{ error }}</p></section>
    <section class="settings-panel">
      <div class="destination-heading">
        <div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>归档目的地</h2><p>每个任务选择一个目的地。WebDAV 文件会先在本地临时写入，远端提交成功后才算完成。</p></div></div>
        <button ref="newWebdavButton" class="primary-button destination-add-button" type="button" @click="openNewWebdav"><Plus :size="17" />添加 WebDAV</button>
      </div>
      <div class="destination-list">
        <div v-for="destination in settings.destinations" :key="destination.id" class="destination-row">
          <div><b>{{ destination.name }}{{ destination.is_system ? '（系统）' : '' }}</b><small>{{ destination.kind === 'WEBDAV' ? destination.webdav_url : destination.local_root }}{{ destination.kind === 'WEBDAV' && destination.remote_root ? '/' + destination.remote_root : '' }}</small></div>
          <span :class="['destination-status', { disabled: !destination.enabled }]">{{ destination.enabled ? '可用' : '已停用' }}</span>
          <div class="destination-row-actions">
            <template v-if="!destination.is_system && destination.kind === 'WEBDAV'">
              <button class="quiet-button" type="button" :disabled="testingDestinationId === destination.id" @click="testSavedDestinationRow(destination.id)">{{ testingDestinationId === destination.id ? '测试中…' : '测试连接' }}</button>
              <button class="quiet-button" type="button" @click="startDestinationEdit(destination, $event.currentTarget)">编辑</button>
            </template>
            <button v-if="!destination.is_system && destination.enabled" class="quiet-button" type="button" @click="disableDestination(destination.id)">停用</button>
            <button v-else-if="!destination.is_system" class="quiet-button" type="button" @click="enableDestination(destination.id)">启用</button>
          </div>
          <p v-if="destinationTestResults[destination.id]" :class="['destination-test-result', destinationTestResults[destination.id].ok ? 'success' : 'error']">{{ destinationTestResults[destination.id].message }}</p>
        </div>
      </div>
      <form class="destination-form" @submit.prevent="saveLocalDestination">
        <p class="destination-form-title">添加本地目录</p>
        <label>名称<input v-model="localDestinationName" placeholder="例如：本地归档" required /></label>
        <label>容器内目录<input v-model="localDestinationRoot" placeholder="/data/archives" required /></label>
        <div class="destination-actions">
          <button class="quiet-button" type="button" :disabled="localDestinationTesting || localDestinationSaving" @click="testLocalDestination">{{ localDestinationTesting ? '测试中…' : '测试目录' }}</button>
          <button class="primary-button" type="submit" :disabled="localDestinationTesting || localDestinationSaving">{{ localDestinationSaving ? '保存中…' : '添加本地目录' }}</button>
        </div>
      </form>
      <p v-if="localDestinationMessage" :class="localDestinationMessageKind === 'success' ? 'success-note' : 'form-error'">{{ localDestinationMessage }}</p>
      <p v-if="destinationSaved" class="success-note"><CheckCircle2 :size="17" />{{ destinationSavedMessage }}</p>
    </section>
    <div v-if="destinationDialogOpen" class="destination-dialog-backdrop">
      <section ref="destinationDialog" class="destination-dialog" role="dialog" aria-modal="true" aria-labelledby="webdav-dialog-title" tabindex="-1" @keydown="handleDestinationDialogKeydown">
        <header class="destination-dialog-header">
          <div class="dialog-icon"><Database :size="22" /></div>
          <div class="destination-dialog-heading"><p class="eyebrow">归档目的地</p><h2 id="webdav-dialog-title">{{ destinationDialogTitle }}</h2><p>{{ editingDestinationId === null ? '填写连接信息并完成真实读写测试后再保存。' : '修改后的配置需要重新测试，测试通过后才会写入。' }}</p></div>
          <button class="dialog-close" type="button" aria-label="关闭 WebDAV 弹窗" :disabled="destinationTesting || destinationSaving" @click="closeDestinationDialog"><X :size="21" /></button>
        </header>
        <form class="destination-dialog-form" @submit.prevent="saveDestination">
          <label class="destination-field-wide">名称<input ref="destinationDialogFirstField" v-model="destinationName" placeholder="例如：NAS WebDAV" autocomplete="off" :disabled="destinationTesting || destinationSaving" required /></label>
          <label class="destination-field-wide">WebDAV URL<input v-model="destinationWebdavUrl" type="url" placeholder="https://nas.example.com/dav" autocomplete="url" :disabled="destinationTesting || destinationSaving" required /></label>
          <label>用户名<input v-model="destinationWebdavUsername" autocomplete="username" :disabled="destinationTesting || destinationSaving" /></label>
          <label>密码<input v-model="destinationWebdavPassword" type="password" autocomplete="new-password" :placeholder="editingDestinationId !== null && editingDestination?.webdav_password_configured ? '********' : ''" :disabled="destinationTesting || destinationSaving" /></label>
          <label class="destination-field-wide">远端根路径<input v-model="destinationRemoteRoot" placeholder="telegram-archive" autocomplete="off" :disabled="destinationTesting || destinationSaving" /></label>
          <p v-if="destinationMessage" :class="['destination-dialog-message', destinationMessageKind]">{{ destinationMessage }}</p>
          <small v-if="!destinationCanSave" class="destination-test-hint">请先测试当前 WebDAV 配置，测试通过后才能保存。</small>
          <div class="destination-dialog-actions">
            <button class="quiet-button" type="button" :disabled="destinationTesting || destinationSaving" @click="testDestination">{{ destinationTesting ? '测试中…' : '测试连接' }}</button>
            <button class="quiet-button" type="button" :disabled="destinationTesting || destinationSaving" @click="closeDestinationDialog">取消</button>
            <button class="primary-button" type="submit" :disabled="destinationTesting || destinationSaving || !destinationCanSave">{{ destinationSaving ? '保存中…' : editingDestinationId !== null ? '保存修改' : '添加 WebDAV' }}</button>
          </div>
        </form>
      </section>
    </div>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>数据卷</h2><p>下载目录由 Docker 挂载决定，应用不会列出宿主机文件系统。</p></div></div><div class="path-box"><span>DOWNLOAD_ROOT</span><code>{{ settings.downloadRoot }}</code></div></section>
    <section class="lan-warning"><ShieldAlert :size="21" /><div><b>可信局域网模式</b><p>{{ settings.trustedLanWarning }}</p></div></section>
  </section>
</template>
