<template>
  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>{{ title }}</h3>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
      <slot name="actions" />
    </div>
    <div ref="chartRef" class="chart" />
  </section>
</template>

<script setup lang="ts">
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{
  title: string
  subtitle?: string
  option: Record<string, unknown>
}>()

const chartRef = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined

function render() {
  if (!chartRef.value) return
  chart ||= echarts.init(chartRef.value)
  chart.setOption(props.option as echarts.EChartsOption, true)
}

function resize() {
  chart?.resize()
}

onMounted(() => {
  render()
  window.addEventListener('resize', resize)
})

watch(() => props.option, render, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
})
</script>
