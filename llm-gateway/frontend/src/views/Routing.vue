<template>
  <div class="grid cols-2">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>路由策略</h3>
          <p>primary、weighted、canary、fallbacks</p>
        </div>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-table v-loading="loading" :data="rows" highlight-current-row @row-click="select">
        <el-table-column prop="name" label="Route" width="160" />
        <el-table-column prop="primary" label="Primary" min-width="200" />
        <el-table-column label="Fallback" width="120">
          <template #default="{ row }">{{ row.fallbacks?.length || 0 }}</template>
        </el-table-column>
        <el-table-column label="灰度" width="90">
          <template #default="{ row }">{{ row.canary?.length || 0 }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>编辑路由</h3>
          <p>保存后走后端热更新接口</p>
        </div>
      </div>
      <el-form label-width="110px">
        <el-form-item label="Route 名称">
          <el-input v-model="routeName" />
        </el-form-item>
        <el-form-item label="Primary">
          <el-input v-model="draft.primary" placeholder="deepseek:deepseek-v4-flash" />
        </el-form-item>
        <el-form-item label="Fallbacks">
          <el-select v-model="draft.fallbacks" multiple filterable allow-create default-first-option style="width: 100%" />
        </el-form-item>
        <el-form-item label="Weighted JSON">
          <el-input v-model="weightedText" type="textarea" :rows="5" />
        </el-form-item>
        <el-form-item label="Canary JSON">
          <el-input v-model="canaryText" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item>
          <el-space>
            <el-button type="primary" @click="save">保存</el-button>
            <el-button type="danger" plain @click="remove">删除</el-button>
          </el-space>
        </el-form-item>
      </el-form>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { deleteRoute, getRoutes, saveRoute, type RouteConfig } from '../api/client'

const loading = ref(false)
const routes = ref<Record<string, RouteConfig>>({})
const routeName = ref('')
const draft = ref<RouteConfig>({ primary: '', fallbacks: [], weighted: [], canary: [] })
const weightedText = ref('[]')
const canaryText = ref('[]')

const rows = computed(() => Object.entries(routes.value).map(([name, value]) => ({ name, ...value })))

function select(row: RouteConfig & { name: string }) {
  routeName.value = row.name
  draft.value = JSON.parse(JSON.stringify(row))
  weightedText.value = JSON.stringify(row.weighted || [], null, 2)
  canaryText.value = JSON.stringify(row.canary || [], null, 2)
}

async function load() {
  loading.value = true
  try {
    routes.value = await getRoutes()
    if (!routeName.value && rows.value[0]) select(rows.value[0])
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!routeName.value || !draft.value.primary) {
    ElMessage.warning('Route 名称和 Primary 必填')
    return
  }
  await saveRoute(routeName.value, {
    ...draft.value,
    weighted: JSON.parse(weightedText.value || '[]'),
    canary: JSON.parse(canaryText.value || '[]')
  })
  ElMessage.success('已保存')
  await load()
}

async function remove() {
  if (!routeName.value) return
  await deleteRoute(routeName.value)
  ElMessage.success('已删除')
  routeName.value = ''
  draft.value = { primary: '', fallbacks: [], weighted: [], canary: [] }
  await load()
}

onMounted(load)
</script>
