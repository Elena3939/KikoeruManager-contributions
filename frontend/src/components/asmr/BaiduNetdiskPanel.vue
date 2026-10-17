<template>
  <HttpDownloadPanel
    provider="baidu"
    :draft="draft"
    @update:draft="$emit('update:draft', $event)"
    @started="(taskIds, meta) => $emit('started', taskIds, meta)"
  />
</template>

<script setup>
import HttpDownloadPanel from './HttpDownloadPanel.vue'

defineProps({
  draft: { type: Object, default: () => ({}) }
})

defineEmits(['started', 'update:draft'])
</script>
