<script setup lang="ts">
import { computed } from 'vue'
import { RefreshCw, X } from 'lucide-vue-next'
import type { DeleteOperation } from '../api'

const props = withDefaults(defineProps<{ operation: DeleteOperation | null; busy?: boolean }>(), { busy: false })
const emit = defineEmits<{ retry: []; close: [] }>()
const canRetry = computed(() => Boolean(props.operation?.items.some((item) => (item.status === 'FAILED' && item.retryable) || item.archive_results?.some((child) => child.status === 'FAILED' && child.retryable))))
</script>

<template>
  <section v-if="props.operation" class="delete-result-panel" role="status" aria-live="polite">
    <button class="delete-result-close" type="button" aria-label="关闭结果" @click="emit('close')"><X :size="17" /></button>
    <strong>{{ props.operation.status === 'PENDING' || props.operation.status === 'RUNNING' ? '删除处理中…' : props.operation.status === 'FAILED' ? '删除完成，但有项目失败' : '删除已完成' }}</strong>
    <span v-if="props.operation.status !== 'PENDING' && props.operation.status !== 'RUNNING'">成功 {{ props.operation.success_count }} 项</span>
    <span v-if="props.operation.failed_count">失败 {{ props.operation.failed_count }} 项</span>
    <span v-if="props.operation.preserved_legacy_count">历史归档保留 {{ props.operation.preserved_legacy_count }} 项</span>
    <button v-if="props.operation.status === 'FAILED' && canRetry" class="quiet-button" type="button" :disabled="props.busy" @click="emit('retry')"><RefreshCw :size="15" />重试失败项</button>
  </section>
</template>
