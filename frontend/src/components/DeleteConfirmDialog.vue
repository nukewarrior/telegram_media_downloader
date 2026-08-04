<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { AlertTriangle, X } from 'lucide-vue-next'

const props = withDefaults(defineProps<{
  open: boolean
  count: number
  subject: string
  busy?: boolean
}>(), { busy: false })

const emit = defineEmits<{ confirm: []; cancel: [] }>()
const dialog = ref<HTMLElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && !props.busy) {
    event.preventDefault()
    emit('cancel')
    return
  }
  if (event.key !== 'Tab') return
  const focusable = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])') ?? [])
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
  cancelButton.value?.focus()
})

function cancel() {
  if (props.busy) return
  restoreFocus?.focus()
  restoreFocus = null
  emit('cancel')
}

onBeforeUnmount(() => restoreFocus?.focus())
</script>

<template>
  <div v-if="open" class="delete-dialog-backdrop" @keydown="onKeydown">
    <section ref="dialog" class="delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-dialog-title" aria-describedby="delete-dialog-description" :aria-busy="busy" tabindex="-1">
      <button class="dialog-close" type="button" aria-label="关闭确认框" :disabled="busy" @click="cancel"><X :size="19" /></button>
      <div class="delete-dialog-icon"><AlertTriangle :size="24" /></div>
      <p class="eyebrow">永久删除确认</p>
      <h2 id="delete-dialog-title">删除 {{ count }} 个{{ subject }}？</h2>
      <p id="delete-dialog-description" class="delete-dialog-copy">此操作永久删除且不可撤销。若物理文件仍被其他归档项或目的地共享，系统会保留共享文件。</p>
      <div class="delete-dialog-actions">
        <button ref="cancelButton" class="quiet-button" type="button" :disabled="busy" @click="cancel">取消</button>
        <button class="danger-button" type="button" :disabled="busy" @click="emit('confirm')">{{ busy ? '删除处理中…' : '永久删除' }}</button>
      </div>
    </section>
  </div>
</template>
