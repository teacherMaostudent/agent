<template>
  <pre class="json-block">{{ pretty(value) }}</pre>
</template>

<script setup lang="ts">
import { pretty } from '../utils/format'

defineProps<{ value: unknown }>()
</script>
