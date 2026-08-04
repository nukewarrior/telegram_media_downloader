<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowRight, CheckCircle2, Wifi } from 'lucide-vue-next'
import { api } from '../api'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: []; connected: [] }>()
const dialog = ref<HTMLElement | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null
const step = ref<'phone' | 'code' | 'password'>('phone')
const phone = ref('')
const code = ref('')
const password = ref('')
const attemptId = ref('')
const loginHint = ref('')
const error = ref('')
const busy = ref(false)

function focusInitialControl() {
  const control = dialog.value?.querySelector<HTMLElement>('input:not([disabled]), select:not([disabled]), button:not([disabled]):not(.dialog-close)')
  ;(control ?? closeButton.value)?.focus()
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && !busy.value) {
    event.preventDefault()
    close()
    return
  }
  if (event.key !== 'Tab') return
  const focusable = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href]') ?? [])
  if (!focusable.length) return
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

watch(() => props.open, async (open) => {
  if (!open) {
    restoreFocus?.focus()
    restoreFocus = null
    return
  }
  restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  await nextTick()
  focusInitialControl()
})

watch([step, busy], async () => {
  if (!props.open || busy.value) return
  await nextTick()
  focusInitialControl()
})

onBeforeUnmount(() => restoreFocus?.focus())

function reset() {
  step.value = 'phone'
  code.value = ''
  password.value = ''
  attemptId.value = ''
  loginHint.value = ''
  error.value = ''
}
function close() {
  if (busy.value) return
  reset()
  emit('close')
}
async function sendCode() {
  error.value = ''; busy.value = true
  try { const result = await api.sendCode(phone.value); attemptId.value = result.attemptId; loginHint.value = result.demoHint ?? ''; step.value = result.passwordRequired ? 'password' : 'code' } catch (reason) { error.value = reason instanceof Error ? reason.message : '无法发送验证码' } finally { busy.value = false }
}
async function verifyCode() {
  error.value = ''; busy.value = true
  try {
    const result = await api.verifyCode(attemptId.value, code.value)
    if (result.passwordRequired) { step.value = 'password'; password.value = ''; return }
    reset(); emit('connected')
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '验证码无效' } finally { busy.value = false }
}
async function verifyPassword() {
  error.value = ''; busy.value = true
  try { await api.verifyPassword(attemptId.value, password.value); reset(); emit('connected') } catch (reason) { error.value = reason instanceof Error ? reason.message : '密码无效' } finally { busy.value = false }
}
</script>

<template>
  <div v-if="open" class="dialog-backdrop" @keydown="onKeydown"><section ref="dialog" class="login-dialog" role="dialog" aria-modal="true" aria-labelledby="telegram-login-title" aria-describedby="telegram-login-copy" :aria-busy="busy"><button ref="closeButton" class="dialog-close" type="button" aria-label="关闭" :disabled="busy" @click="close">×</button><div class="dialog-icon"><Wifi :size="22" /></div><template v-if="step === 'phone'"><h2 id="telegram-login-title">连接 Telegram</h2><p id="telegram-login-copy">输入带国家区号的手机号。验证码仅用于本次登录，不会保存在本服务中。</p><label>手机号<input v-model="phone" placeholder="+86 138 0000 0000" autocomplete="tel" :disabled="busy" autofocus /></label><button class="primary-button wide" type="button" :disabled="busy" @click="sendCode">{{ busy ? '正在发送…' : '发送验证码' }}<ArrowRight v-if="!busy" :size="18" /></button></template><template v-else-if="step === 'code'"><h2 id="telegram-login-title">输入验证码</h2><p id="telegram-login-copy">验证码已发送到 {{ phone }}。服务重启或超过十分钟后，请重新发送。</p><label>验证码<input v-model="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="123456" :disabled="busy" autofocus /></label><small v-if="loginHint" class="tip">{{ loginHint }}</small><button class="primary-button wide" type="button" :disabled="busy" @click="verifyCode">{{ busy ? '正在验证…' : '验证并连接' }}<CheckCircle2 v-if="!busy" :size="18" /></button><button class="text-button" type="button" :disabled="busy" @click="sendCode">重新发送验证码</button></template><template v-else><h2 id="telegram-login-title">两步验证</h2><p id="telegram-login-copy">此账号启用了额外密码保护。密码仅用于本次验证。</p><label>两步验证密码<input v-model="password" type="password" autocomplete="current-password" :disabled="busy" autofocus /></label><button class="primary-button wide" type="button" :disabled="busy" @click="verifyPassword">{{ busy ? '正在验证…' : '完成连接' }}<CheckCircle2 v-if="!busy" :size="18" /></button></template><p v-if="error" class="form-error" role="alert">{{ error }}</p></section></div>
</template>
