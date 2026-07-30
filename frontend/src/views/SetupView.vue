<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Archive, ArrowRight, CheckCircle2, KeyRound, LockKeyhole } from 'lucide-vue-next'
import { api } from '../api'

const emit = defineEmits<{ configured: [] }>()
const router = useRouter()
const apiId = ref('')
const apiHash = ref('')
const error = ref('')
const saving = ref(false)

async function submit() {
  error.value = ''
  saving.value = true
  try {
    await api.setup(apiId.value.trim(), apiHash.value.trim())
    emit('configured')
    await router.replace('/tasks')
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '保存失败'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <main class="setup-shell">
    <section class="setup-aside">
      <div class="brand"><span class="brand-mark"><Archive :size="20" /></span><span>Telegram 媒体归档</span></div>
      <div class="setup-copy"><span class="eyebrow">首次配置</span><h1>先连接你的<br />Telegram 开发凭据。</h1><p>凭据只会保存在服务端数据卷中，浏览器不会再次读取 API Hash。</p></div>
      <ol class="setup-steps"><li class="active"><CheckCircle2 :size="18" /><span>配置 API 凭据</span></li><li><span>2</span><span>连接 Telegram 账号</span></li><li><span>3</span><span>开始创建归档</span></li></ol>
    </section>
    <section class="setup-main">
      <form class="setup-form" @submit.prevent="submit">
        <div><span class="eyebrow">步骤 1 / 3</span><h2>配置 Telegram API</h2><p>在 <a href="https://my.telegram.org" target="_blank" rel="noreferrer">my.telegram.org</a> 创建应用后填写凭据。</p></div>
        <label>API ID<input v-model="apiId" inputmode="numeric" autocomplete="off" placeholder="例如：12345678" required /></label>
        <label>API Hash<div class="input-with-icon"><KeyRound :size="18" /><input v-model="apiHash" type="password" autocomplete="off" placeholder="32 位 API Hash" required /></div></label>
        <div class="security-callout"><LockKeyhole :size="18" /><span>API Hash、验证码和两步验证密码不会回显或写入应用日志。此服务未启用访问认证，仅可用于可信局域网，绝不可暴露公网。</span></div>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button class="primary-button wide" :disabled="saving">{{ saving ? '正在保存…' : '保存并进入任务中心' }}<ArrowRight :size="18" /></button>
      </form>
    </section>
  </main>
</template>
