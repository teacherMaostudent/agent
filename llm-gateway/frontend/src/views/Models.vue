<template>
  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>Provider 与模型</h3>
        <p>协议、baseUrl、密钥状态和已注册模型</p>
      </div>
      <el-space>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Aim" @click="probe">探测</el-button>
      </el-space>
    </div>
    <el-table v-loading="loading" :data="rows">
      <el-table-column prop="name" label="Provider" width="150" />
      <el-table-column prop="protocol" label="协议" width="170">
        <template #default="{ row }"><el-tag effect="plain">{{ row.protocol }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="baseUrl" label="Base URL" min-width="280" show-overflow-tooltip />
      <el-table-column prop="apiKeyConfigured" label="密钥" width="100">
        <template #default="{ row }">
          <el-tag :type="row.apiKeyConfigured ? 'success' : 'danger'">{{ row.apiKeyConfigured ? '已配置' : '缺失' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="模型">
        <template #default="{ row }">
          <el-space wrap>
            <el-tag v-for="model in row.models" :key="model">{{ model }}</el-tag>
          </el-space>
        </template>
      </el-table-column>
    </el-table>
  </section>
  <section class="panel">
    <div class="panel-head"><h3>健康探测结果</h3></div>
    <JsonBlock :value="probeResult" />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Aim, Refresh } from '@element-plus/icons-vue'
import { getProviders, probeModels } from '../api/client'
import JsonBlock from '../components/JsonBlock.vue'

const loading = ref(false)
const providers = ref<Record<string, any>>({})
const probeResult = ref({})
const rows = computed(() => Object.entries(providers.value).map(([name, value]) => ({ name, ...value })))

async function load() {
  loading.value = true
  try {
    providers.value = await getProviders()
  } finally {
    loading.value = false
  }
}

async function probe() {
  probeResult.value = await probeModels()
}

onMounted(load)
</script>
