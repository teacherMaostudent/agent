<template>
  <div class="grid cols-2">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>Prompt 模板</h3>
          <p>system/user 模板和变量渲染入口</p>
        </div>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-table v-loading="loading" :data="rows" highlight-current-row @row-click="select">
        <el-table-column prop="id" label="模板 ID" width="180" />
        <el-table-column prop="system" label="System" show-overflow-tooltip />
        <el-table-column prop="user" label="User" show-overflow-tooltip />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head"><h3>模板预览</h3></div>
      <el-tabs>
        <el-tab-pane label="System"><pre class="json-block light">{{ selected?.system || '' }}</pre></el-tab-pane>
        <el-tab-pane label="User"><pre class="json-block light">{{ selected?.user || '' }}</pre></el-tab-pane>
        <el-tab-pane label="请求示例">
          <JsonBlock :value="samplePayload" />
        </el-tab-pane>
      </el-tabs>
    </section>
  </div>

  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>Prompt 治理资产</h3>
        <p>bad case、输出契约、后处理链和 memory 隔离</p>
      </div>
      <el-button @click="loadEngineering">加载治理资产</el-button>
    </div>
    <JsonBlock :value="engineering" />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { getEngineering, getPromptTemplates, type PromptTemplate } from '../api/client'
import JsonBlock from '../components/JsonBlock.vue'

const loading = ref(false)
const templates = ref<Record<string, PromptTemplate>>({})
const selectedId = ref('')
const engineering = ref({})

const rows = computed(() => Object.entries(templates.value).map(([id, value]) => ({ id, ...value })))
const selected = computed(() => templates.value[selectedId.value])
const samplePayload = computed(() => ({
  model: 'deepseek-v4-flash',
  prompt_template: selectedId.value || 'interview-answer',
  variables: {
    topic: '模型路由',
    project: 'LLM Gateway'
  }
}))

function select(row: PromptTemplate & { id: string }) {
  selectedId.value = row.id
}

async function load() {
  loading.value = true
  try {
    templates.value = await getPromptTemplates()
    if (!selectedId.value && rows.value[0]) selectedId.value = rows.value[0].id
  } finally {
    loading.value = false
  }
}

async function loadEngineering() {
  engineering.value = await getEngineering()
}

onMounted(load)
</script>

<style scoped>
.light {
  background: #f8fafc;
  color: #263241;
}
</style>
