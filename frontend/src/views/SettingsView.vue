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
const sourceCacheMiB = ref(2048)
const archiveTimezone = ref('UTC')
const timezoneSearch = ref('')
const timezones = ref<string[]>([])
const saved = ref(false)
const concurrencySaved = ref(false)
const sourceCacheSaved = ref(false)
const timezoneSaved = ref(false)
const destinationKind = ref<'LOCAL' | 'WEBDAV'>('WEBDAV')
const destinationName = ref('')
const destinationLocalRoot = ref('')
const destinationWebdavUrl = ref('')
const destinationWebdavUsername = ref('')
const destinationWebdavPassword = ref('')
const destinationRemoteRoot = ref('telegram-archive')
const destinationSaving = ref(false)
const destinationTesting = ref(false)
const destinationSaved = ref(false)
const destinationMessage = ref('')
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
  sourceCacheMiB.value = Math.round(result.sourceCache.maxBytes / (1024 * 1024))
  archiveTimezone.value = result.archiveTimezone ?? 'UTC'
  timezones.value = timezoneResult.timezones
}
async function save() { error.value = ''; try { await api.updateApi(apiId.value, apiHash.value); edit.value = false; saved.value = true; await load(); window.setTimeout(() => saved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveConcurrency() { error.value = ''; try { await api.updateDownloadConcurrency(concurrency.value); concurrencySaved.value = true; await load(); window.setTimeout(() => concurrencySaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveSourceCache() { error.value = ''; try { await api.updateSourceCache(sourceCacheMiB.value * 1024 * 1024); sourceCacheSaved.value = true; await load(); window.setTimeout(() => sourceCacheSaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
async function saveTimezone() { error.value = ''; try { await api.updateArchiveTimezone(archiveTimezone.value); timezoneSaved.value = true; emit('state-changed'); await load(); window.setTimeout(() => timezoneSaved.value = false, 2500) } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
function destinationPayload() { return { name: destinationName.value, kind: destinationKind.value, local_root: destinationKind.value === 'LOCAL' ? destinationLocalRoot.value : null, webdav_url: destinationKind.value === 'WEBDAV' ? destinationWebdavUrl.value : null, webdav_username: destinationKind.value === 'WEBDAV' ? destinationWebdavUsername.value || null : null, webdav_password: destinationKind.value === 'WEBDAV' ? destinationWebdavPassword.value || null : null, remote_root: destinationKind.value === 'WEBDAV' ? destinationRemoteRoot.value : '', enabled: true } }
async function testDestination() { destinationTesting.value = true; destinationMessage.value = ''; try { destinationMessage.value = (await api.testDestination(destinationPayload())).message } catch (reason) { destinationMessage.value = reason instanceof Error ? reason.message : '连接测试失败' } finally { destinationTesting.value = false } }
async function saveDestination() { destinationSaving.value = true; destinationMessage.value = ''; try { await api.createDestination(destinationPayload()); destinationSaved.value = true; destinationName.value = ''; destinationWebdavUrl.value = ''; destinationWebdavUsername.value = ''; destinationWebdavPassword.value = ''; await load(); window.setTimeout(() => destinationSaved.value = false, 2500) } catch (reason) { destinationMessage.value = reason instanceof Error ? reason.message : '保存失败' } finally { destinationSaving.value = false } }
async function disableDestination(id: number) { destinationMessage.value = ''; try { await api.disableDestination(id); await load() } catch (reason) { destinationMessage.value = reason instanceof Error ? reason.message : '停用失败' } }
async function enableDestination(id: number) { destinationMessage.value = ''; try { await api.enableDestination(id); await load() } catch (reason) { destinationMessage.value = reason instanceof Error ? reason.message : '启用失败' } }
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
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>来源浏览缓存</h2><p>已浏览媒体的索引会长期保留；超出上限时自动淘汰最久未访问的图片和视频缩略图。</p></div></div><form class="setting-row" @submit.prevent="saveSourceCache"><label>缩略图缓存上限（MiB）<input v-model.number="sourceCacheMiB" type="number" min="256" max="20480" required /></label><div><button class="primary-button" type="submit">保存</button><small>默认 2048 MiB；不会下载完整来源文件。</small></div></form><p v-if="sourceCacheSaved" class="success-note"><CheckCircle2 :size="17" />来源缓存上限已保存</p></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><KeyRound :size="20" /></div><div><h2>Telegram API 凭据</h2><p>API Hash 配置后仅能更新，永不回显。</p></div></div><template v-if="!edit"><div class="setting-row"><div><b>API ID：{{ settings.apiId }}</b><small>API Hash：已配置 <EyeOff :size="14" /></small></div><button class="quiet-button" @click="edit = true"><RefreshCw :size="16" />更新凭据</button></div></template><form v-else class="credentials-form" @submit.prevent="save"><label>新的 API ID<input v-model="apiId" required /></label><label>新的 API Hash<input v-model="apiHash" type="password" required /></label><div><button class="quiet-button" type="button" @click="edit = false">取消</button><button class="primary-button" type="submit">安全保存</button></div></form><p v-if="saved" class="success-note"><CheckCircle2 :size="17" />凭据已更新</p><p v-if="error" class="form-error">{{ error }}</p></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>归档目的地</h2><p>每个任务选择一个目的地。WebDAV 文件会先在本地临时写入，远端提交成功后才算完成。</p></div></div><div class="destination-list"><div v-for="destination in settings.destinations" :key="destination.id" class="destination-row"><div><b>{{ destination.name }}{{ destination.is_system ? '（系统）' : '' }}</b><small>{{ destination.kind === 'WEBDAV' ? destination.webdav_url : destination.local_root }}{{ destination.kind === 'WEBDAV' && destination.remote_root ? '/' + destination.remote_root : '' }}</small></div><span class="destination-status">{{ destination.enabled ? '可用' : '已停用' }}</span><button v-if="!destination.is_system && destination.enabled" class="quiet-button" type="button" @click="disableDestination(destination.id)">停用</button><button v-else-if="!destination.is_system" class="quiet-button" type="button" @click="enableDestination(destination.id)">启用</button></div></div><form class="destination-form" @submit.prevent="saveDestination"><label>名称<input v-model="destinationName" placeholder="例如：NAS WebDAV" required /></label><label>类型<select v-model="destinationKind"><option value="WEBDAV">WebDAV</option><option value="LOCAL">本地目录</option></select></label><label v-if="destinationKind === 'LOCAL'">容器内目录<input v-model="destinationLocalRoot" placeholder="/data/archives" required /></label><template v-else><label>WebDAV URL<input v-model="destinationWebdavUrl" type="url" placeholder="https://nas.example.com/dav" required /></label><label>用户名<input v-model="destinationWebdavUsername" autocomplete="username" /></label><label>密码<input v-model="destinationWebdavPassword" type="password" autocomplete="new-password" /></label><label>远端根路径<input v-model="destinationRemoteRoot" placeholder="telegram-archive" /></label></template><div class="destination-actions"><button class="quiet-button" type="button" :disabled="destinationTesting" @click="testDestination">{{ destinationTesting ? '测试中…' : '测试连接' }}</button><button class="primary-button" type="submit" :disabled="destinationSaving">{{ destinationSaving ? '保存中…' : '添加目的地' }}</button></div></form><p v-if="destinationMessage" class="form-error">{{ destinationMessage }}</p><p v-if="destinationSaved" class="success-note"><CheckCircle2 :size="17" />目的地已添加</p></section>
    <section class="settings-panel"><div class="settings-heading"><div class="settings-icon"><Database :size="20" /></div><div><h2>数据卷</h2><p>下载目录由 Docker 挂载决定，应用不会列出宿主机文件系统。</p></div></div><div class="path-box"><span>DOWNLOAD_ROOT</span><code>{{ settings.downloadRoot }}</code></div></section>
    <section class="lan-warning"><ShieldAlert :size="21" /><div><b>可信局域网模式</b><p>{{ settings.trustedLanWarning }}</p></div></section>
  </section>
</template>
