<template>
  <div class="grid cols-2">
    <ChartPanel title="成本报表" subtitle="调用成本和 token 消耗" :option="costOption" />
    <ChartPanel title="性能报表" subtitle="延迟、吞吐和失败率" :option="perfOption" />
  </div>
  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>原始报表</h3>
        <p>后端返回的 cost/performance/engineering 快照</p>
      </div>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>
    <el-tabs>
      <el-tab-pane label="Cost"><JsonBlock :value="cost" /></el-tab-pane>
      <el-tab-pane label="Performance"><JsonBlock :value="performance" /></el-tab-pane>
      <el-tab-pane label="Engineering"><JsonBlock :value="engineering" /></el-tab-pane>
    </el-tabs>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import ChartPanel from '../components/ChartPanel.vue'
import JsonBlock from '../components/JsonBlock.vue'
import { getCostReport, getEngineering, getPerformanceReport } from '../api/client'

const cost = ref<Record<string, any>>({})
const performance = ref<Record<string, any>>({})
const engineering = ref<Record<string, any>>({})

const costRows = computed(() => Object.entries(cost.value).map(([name, value]) => ({
  name,
  tokens: Number((value as any)?.totalTokens || (value as any)?.tokens || 0),
  cost: Number((value as any)?.costEstimated || (value as any)?.cost || 0)
})))

const costOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: {},
  xAxis: { type: 'category', data: costRows.value.map((row) => row.name) },
  yAxis: { type: 'value' },
  series: [
    { name: 'tokens', type: 'bar', data: costRows.value.map((row) => row.tokens) },
    { name: 'cost', type: 'line', data: costRows.value.map((row) => row.cost) }
  ]
}))

const perfRows = computed(() => Object.entries(performance.value).map(([name, value]) => ({
  name,
  latency: Number((value as any)?.averageLatencyMs || (value as any)?.latencyMs || 0),
  errors: Number((value as any)?.errorRate || (value as any)?.errors || 0)
})))

const perfOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  xAxis: { type: 'category', data: perfRows.value.map((row) => row.name) },
  yAxis: { type: 'value' },
  series: [
    { name: 'latency', type: 'bar', data: perfRows.value.map((row) => row.latency) },
    { name: 'errors', type: 'line', data: perfRows.value.map((row) => row.errors) }
  ]
}))

async function load() {
  const [costData, perfData, engData] = await Promise.all([getCostReport(), getPerformanceReport(), getEngineering()])
  cost.value = costData
  performance.value = perfData
  engineering.value = engData
}

onMounted(load)
</script>
