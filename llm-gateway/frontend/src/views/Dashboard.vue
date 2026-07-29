<template>
  <el-skeleton v-if="loading" :rows="8" animated />
  <template v-else>
    <div class="grid cols-3">
      <div class="metric"><span>Provider 数</span><strong>{{ providerCount }}</strong></div>
      <div class="metric"><span>路由策略</span><strong>{{ routeCount }}</strong></div>
      <div class="metric"><span>缓存条目</span><strong>{{ cacheCount }}</strong></div>
    </div>

    <div class="grid cols-2" style="margin-top: 16px">
      <ChartPanel title="模型成本分布" subtitle="按当前 cost report 聚合" :option="costOption" />
      <ChartPanel title="性能指标快照" subtitle="TTFT / TPOT / Tokens/s / 错误率" :option="perfOption" />
    </div>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>模型健康状态</h3>
          <p>来自熔断、限流和探测状态快照</p>
        </div>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-table :data="healthRows" size="small">
        <el-table-column prop="route" label="Route" min-width="180" />
        <el-table-column prop="state" label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="row.state === 'healthy' ? 'success' : 'warning'">{{ row.state }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="failures" label="失败次数" width="120" />
        <el-table-column prop="raw" label="原始快照" min-width="360" show-overflow-tooltip />
      </el-table>
    </section>
  </template>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import ChartPanel from '../components/ChartPanel.vue'
import { getOverview } from '../api/client'
import { objectSize } from '../utils/format'

const loading = ref(false)
const overview = ref<Record<string, any>>({})

const providerCount = computed(() => Array.isArray(overview.value.providers) ? overview.value.providers.length : 0)
const routeCount = computed(() => Array.isArray(overview.value.routes) ? overview.value.routes.length : 0)
const cacheCount = computed(() => objectSize(overview.value.cache?.entries || overview.value.cache))

const costRows = computed(() => Object.entries(overview.value.costReport || {}).map(([name, value]) => ({
  name,
  value: Number((value as any)?.costEstimated || (value as any)?.cost || value || 0)
})))

const costOption = computed(() => ({
  tooltip: { trigger: 'item' },
  series: [{
    type: 'pie',
    radius: ['45%', '72%'],
    data: costRows.value.length ? costRows.value : [{ name: 'no data', value: 1 }],
    label: { formatter: '{b}' }
  }]
}))

const perfRows = computed(() => Object.entries(overview.value.performance || {}).slice(0, 6).map(([name, value]) => ({
  name,
  ttft: Number((value as any)?.averageTtftMs || (value as any)?.ttftMs || 0),
  tpot: Number((value as any)?.averageTpotMs || (value as any)?.tpotMs || 0),
  tokens: Number((value as any)?.tokensPerSecond || 0)
})))

const perfOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { top: 0 },
  grid: { left: 40, right: 20, top: 45, bottom: 40 },
  xAxis: { type: 'category', data: perfRows.value.map((row) => row.name) },
  yAxis: { type: 'value' },
  series: [
    { name: 'TTFT', type: 'bar', data: perfRows.value.map((row) => row.ttft) },
    { name: 'TPOT', type: 'bar', data: perfRows.value.map((row) => row.tpot) },
    { name: 'Tokens/s', type: 'line', data: perfRows.value.map((row) => row.tokens) }
  ]
}))

const healthRows = computed(() => {
  const source = overview.value.resilience || {}
  return Object.entries(source).map(([route, value]) => ({
    route,
    state: (value as any)?.state || (value as any)?.open ? 'degraded' : 'healthy',
    failures: (value as any)?.failures || (value as any)?.failureCount || 0,
    raw: JSON.stringify(value)
  }))
})

async function load() {
  loading.value = true
  try {
    overview.value = await getOverview()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
