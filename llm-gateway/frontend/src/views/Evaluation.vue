<template>
  <div class="evaluation-page">
    <div class="grid cols-2">
      <section class="panel">
        <div class="panel-head">
          <div>
            <h3>评估资产</h3>
            <p>Prompt、检索策略、Golden Dataset、Rubric 和运行记录</p>
          </div>
          <el-button :icon="Refresh" @click="load">刷新</el-button>
        </div>
        <JsonBlock :value="snapshot" />
      </section>
      <ChartPanel title="LLM Judge 质量趋势" subtitle="平均分、通过率与仲裁率" :option="judgeOption" />
    </div>

    <section class="panel judge-runs">
      <div class="panel-head">
        <div>
          <h3>Judge Runs</h3>
          <p>双裁判结构化评分，分歧超过阈值时调用第三模型仲裁</p>
        </div>
      </div>
      <el-table :data="judgeRuns" size="small">
        <el-table-column prop="id" label="Run ID" min-width="220" show-overflow-tooltip />
        <el-table-column prop="candidateModel" label="被测模型" min-width="150" />
        <el-table-column label="平均分" width="100">
          <template #default="{ row }">{{ row.metrics?.averageScore ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="通过率" width="100">
          <template #default="{ row }">{{ percent(row.metrics?.passRate) }}</template>
        </el-table-column>
        <el-table-column label="仲裁率" width="100">
          <template #default="{ row }">{{ percent(row.metrics?.arbitrationRate) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="row.status === 'COMPLETED' ? 'success' : 'danger'">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="timestamp" label="运行时间" min-width="190" />
      </el-table>
    </section>

    <section class="panel quality-gates">
      <div class="panel-head">
        <div>
          <h3>CI 质量门禁</h3>
          <p>确定性阈值判断；exitCode 1 会阻断流水线</p>
        </div>
      </div>
      <el-table :data="qualityGates" size="small">
        <el-table-column prop="runId" label="Run ID" min-width="220" show-overflow-tooltip />
        <el-table-column label="结论" width="100">
          <template #default="{ row }">
            <el-tag :type="row.passed ? 'success' : 'danger'">{{ row.passed ? 'PASS' : 'BLOCK' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="exitCode" label="Exit Code" width="110" />
        <el-table-column label="原因" min-width="320" show-overflow-tooltip>
          <template #default="{ row }">{{ (row.reasons || []).join('; ') || '全部阈值通过' }}</template>
        </el-table-column>
        <el-table-column prop="timestamp" label="判定时间" min-width="190" />
      </el-table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>线上失败分流</h3>
          <p>高置信度自动归档，中置信度人工审核，低置信度进入抽检池</p>
        </div>
      </div>
      <el-table :data="onlineSamples" size="small">
        <el-table-column prop="id" label="Request ID" min-width="210" show-overflow-tooltip />
        <el-table-column prop="model" label="模型" min-width="140" />
        <el-table-column prop="classification" label="分类" min-width="180" />
        <el-table-column prop="failureConfidence" label="失败置信度" width="120" />
        <el-table-column prop="disposition" label="流转状态" min-width="190" />
        <el-table-column label="规则" width="90">
          <template #default="{ row }">
            <el-tag :type="row.ruleValidation?.passed ? 'success' : 'danger'">
              {{ row.ruleValidation?.passed ? 'PASS' : 'FAIL' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="updatedAt" label="更新时间" min-width="190" />
      </el-table>
    </section>

    <div class="grid cols-2">
      <section class="panel">
        <div class="panel-head">
          <div><h3>人工审核队列</h3><p>中置信度和 Judge 异常样本</p></div>
        </div>
        <el-table :data="humanReviewQueue" size="small">
          <el-table-column prop="id" label="Request ID" min-width="190" show-overflow-tooltip />
          <el-table-column prop="classification" label="分类" min-width="170" />
          <el-table-column prop="failureConfidence" label="置信度" width="100" />
        </el-table>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div><h3>Golden 候选</h3><p>高风险必须专家审批，普通候选也不会自动发布</p></div>
        </div>
        <el-table :data="goldenCandidates" size="small">
          <el-table-column prop="question" label="问题" min-width="210" show-overflow-tooltip />
          <el-table-column prop="classification" label="来源" min-width="160" />
          <el-table-column label="风险" width="90">
            <template #default="{ row }">
              <el-tag :type="row.highRisk ? 'danger' : 'info'">{{ row.highRisk ? 'HIGH' : 'NORMAL' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="状态" min-width="180" />
        </el-table>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import ChartPanel from '../components/ChartPanel.vue'
import JsonBlock from '../components/JsonBlock.vue'
import { getEvaluation, getEvaluationGovernance } from '../api/client'

const snapshot = ref<Record<string, any>>({})
const governance = ref<Record<string, any>>({})
const judgeRuns = computed(() => Array.isArray(snapshot.value.judgeRuns) ? snapshot.value.judgeRuns : [])
const qualityGates = computed(() => Array.isArray(snapshot.value.qualityGates) ? snapshot.value.qualityGates : [])
const onlineSamples = computed(() => Array.isArray(governance.value.samples) ? governance.value.samples : [])
const humanReviewQueue = computed(() => Array.isArray(governance.value.humanReviewQueue) ? governance.value.humanReviewQueue : [])
const goldenCandidates = computed(() => Array.isArray(governance.value.goldenCandidates) ? governance.value.goldenCandidates : [])

const judgeOption = computed(() => {
  const runs = [...judgeRuns.value].sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)))
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0 },
    grid: { left: 45, right: 25, top: 48, bottom: 38 },
    xAxis: { type: 'category', data: runs.map((run) => String(run.id || '').slice(0, 8)) },
    yAxis: { type: 'value', min: 0, max: 100 },
    series: [
      { name: '平均分', type: 'line', data: runs.map((run) => Number(run.metrics?.averageScore || 0)) },
      { name: '通过率', type: 'bar', data: runs.map((run) => Number(run.metrics?.passRate || 0) * 100) },
      { name: '仲裁率', type: 'bar', data: runs.map((run) => Number(run.metrics?.arbitrationRate || 0) * 100) }
    ]
  }
})

function percent(value: unknown) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`
}

async function load() {
  const [evaluationSnapshot, governanceSnapshot] = await Promise.all([getEvaluation(), getEvaluationGovernance()])
  snapshot.value = evaluationSnapshot
  governance.value = governanceSnapshot
}

onMounted(load)
</script>

<style scoped>
.evaluation-page {
  display: grid;
  gap: 16px;
}
</style>
