<template>
  <div class="grid cols-2">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>发起 GMP 审查</h3>
          <p>Java 网关保存任务状态，专业 RAG、OCR 和 Agent 流程交给 rag-agent-service。</p>
        </div>
        <el-tag effect="plain">/v1/gmp/reviews</el-tag>
      </div>

      <el-form label-width="96px">
        <el-form-item label="业务编号">
          <el-input v-model="form.businessId" placeholder="例如 GMP-DOC-001" />
        </el-form-item>
        <el-form-item label="文档 ID">
          <el-input v-model="form.documentId" placeholder="上传后可填 rag-agent-service 返回的 documentId" />
        </el-form-item>
        <el-form-item label="文档类型">
          <el-input v-model="form.documentType" placeholder="例如 场地管理文件管理" />
        </el-form-item>
        <el-form-item label="模型">
          <el-input v-model="form.model" placeholder="例如 deepseek-v4-flash" />
        </el-form-item>
        <el-form-item label="Checklist">
          <el-input v-model="form.checklistVersion" placeholder="例如 2026-07-seed-v1" />
        </el-form-item>
        <el-form-item label="复核提示">
          <el-input v-model="form.reviewerHint" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="文本内容">
          <el-input v-model="form.content" type="textarea" :rows="7" placeholder="没有 documentId 时，可直接粘贴待审查文本。" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="reviewing" @click="submitReview">发起审查</el-button>
          <el-button @click="fillSample">填入示例</el-button>
        </el-form-item>
      </el-form>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>上传文件</h3>
          <p>文件转发给 rag-agent-service，返回结果可作为后续审查输入。</p>
        </div>
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-upload
        drag
        :auto-upload="false"
        :limit="1"
        :on-change="onFileChange"
        :on-remove="onFileRemove"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">拖拽文件到这里，或点击选择</div>
      </el-upload>
      <div class="upload-actions">
        <el-button type="primary" plain :disabled="!selectedFile" :loading="uploading" @click="upload">上传到 RAG 服务</el-button>
      </div>
      <JsonBlock :value="uploadResult" />
    </section>
  </div>

  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>审查任务</h3>
        <p>状态、风险等级、成本和人工复核入口。</p>
      </div>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>
    <el-table :data="tasks" height="360" @row-click="selectTask">
      <el-table-column prop="taskId" label="任务 ID" min-width="220" />
      <el-table-column prop="documentType" label="文档类型" min-width="150" />
      <el-table-column prop="status" label="状态" width="160">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="riskLevel" label="风险" width="110">
        <template #default="{ row }">
          <el-tag :type="riskType(row.riskLevel)">{{ row.riskLevel || '-' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="cost" label="成本" width="110" />
      <el-table-column prop="latencyMs" label="耗时 ms" width="110" />
      <el-table-column prop="updatedAt" label="更新时间" min-width="180" />
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-space>
            <el-button size="small" @click.stop="refresh(row.taskId)">同步</el-button>
            <el-button size="small" plain @click.stop="rerun(row.taskId)">重跑</el-button>
          </el-space>
        </template>
      </el-table-column>
    </el-table>
  </section>

  <div class="grid cols-2" style="margin-top: 16px">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>任务详情</h3>
          <p>包含 rag-agent-service 原始响应，便于排查检索证据、CAPA 和报告字段。</p>
        </div>
        <el-tag v-if="selected?.needHumanReview" type="warning">需要人工复核</el-tag>
      </div>
      <JsonBlock :value="selected || {}" />
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>人工复核</h3>
          <p>Java 网关记录最终确认、驳回、人工修改和审计痕迹。</p>
        </div>
      </div>
      <el-form label-width="92px">
        <el-form-item label="复核人">
          <el-input v-model="confirmForm.reviewer" />
        </el-form-item>
        <el-form-item label="动作">
          <el-select v-model="confirmForm.action">
            <el-option label="确认通过" value="APPROVED" />
            <el-option label="驳回" value="REJECTED" />
            <el-option label="继续复核" value="NEED_HUMAN_REVIEW" />
          </el-select>
        </el-form-item>
        <el-form-item label="风险等级">
          <el-select v-model="confirmForm.finalRiskLevel" clearable>
            <el-option label="LOW" value="LOW" />
            <el-option label="MEDIUM" value="MEDIUM" />
            <el-option label="HIGH" value="HIGH" />
            <el-option label="CRITICAL" value="CRITICAL" />
          </el-select>
        </el-form-item>
        <el-form-item label="最终摘要">
          <el-input v-model="confirmForm.finalSummary" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="复核备注">
          <el-input v-model="confirmForm.notes" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :disabled="!selected" :loading="confirming" @click="confirm">提交复核</el-button>
        </el-form-item>
      </el-form>
    </section>
  </div>

  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>GMP 审计快照</h3>
        <p>任务状态、RAG 调用成本和人工复核记录。</p>
      </div>
    </div>
    <JsonBlock :value="snapshot" />
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Refresh, UploadFilled } from '@element-plus/icons-vue'
import type { UploadFile } from 'element-plus'
import { ElMessage } from 'element-plus'
import JsonBlock from '../components/JsonBlock.vue'
import {
  confirmGmpReview,
  getGmpReviews,
  getGmpSnapshot,
  refreshGmpReview,
  rerunGmpReview,
  startGmpReview,
  uploadGmpDocument,
  type GmpReviewTask
} from '../api/client'

const form = reactive({
  businessId: 'GMP-DOC-001',
  documentId: '',
  documentType: '场地管理文件管理',
  model: 'deepseek-v4-flash',
  checklistVersion: '2026-07-seed-v1',
  reviewerHint: '重点检查文件控制、职责权限、PDCA 和 ALCOA+ 数据可靠性。',
  content: ''
})

const confirmForm = reactive({
  reviewer: 'qa-manager',
  action: 'APPROVED',
  finalRiskLevel: '',
  finalSummary: '',
  notes: ''
})

const tasks = ref<GmpReviewTask[]>([])
const selected = ref<GmpReviewTask | null>(null)
const snapshot = ref<Record<string, unknown>>({})
const uploadResult = ref<Record<string, unknown>>({})
const selectedFile = ref<File | null>(null)
const reviewing = ref(false)
const uploading = ref(false)
const confirming = ref(false)

function fillSample() {
  form.content = '药品生产场地管理文件属于公司质量管理体系文件的一部分，文件包含编号、版本号、生效日期和批准人，但未明确起草人、审核人、分发、修订、回收和作废流程。质量保证部门负责定期审核场地管理文件。'
}

function onFileChange(uploadFile: UploadFile) {
  selectedFile.value = uploadFile.raw || null
}

function onFileRemove() {
  selectedFile.value = null
}

async function upload() {
  if (!selectedFile.value) return
  uploading.value = true
  try {
    uploadResult.value = await uploadGmpDocument(selectedFile.value, form.businessId, form.documentType)
    const documentId = (uploadResult.value as any).documentId || (uploadResult.value as any).document_id || (uploadResult.value as any).id
    if (documentId) {
      form.documentId = String(documentId)
    }
    ElMessage.success('文件已转发给 rag-agent-service')
  } finally {
    uploading.value = false
  }
}

async function submitReview() {
  reviewing.value = true
  try {
    const task = await startGmpReview({
      businessId: form.businessId,
      documentId: form.documentId,
      documentType: form.documentType,
      model: form.model,
      checklistVersion: form.checklistVersion,
      reviewerHint: form.reviewerHint,
      content: form.content,
      metadata: { source: 'llm-gateway-admin-console' }
    })
    selected.value = task
    await load()
    ElMessage.success('GMP 审查任务已创建')
  } finally {
    reviewing.value = false
  }
}

async function load() {
  const [reviewRows, snap] = await Promise.all([getGmpReviews(), getGmpSnapshot()])
  tasks.value = reviewRows
  snapshot.value = snap
  if (!selected.value && reviewRows.length) {
    selected.value = reviewRows[0]
  }
}

function selectTask(row: GmpReviewTask) {
  selected.value = row
  confirmForm.finalRiskLevel = row.riskLevel || ''
  confirmForm.finalSummary = row.summary || ''
}

async function refresh(taskId: string) {
  selected.value = await refreshGmpReview(taskId)
  await load()
}

async function rerun(taskId: string) {
  selected.value = await rerunGmpReview(taskId)
  await load()
}

async function confirm() {
  if (!selected.value) return
  confirming.value = true
  try {
    selected.value = await confirmGmpReview(selected.value.taskId, {
      reviewer: confirmForm.reviewer,
      action: confirmForm.action,
      finalRiskLevel: confirmForm.finalRiskLevel,
      finalSummary: confirmForm.finalSummary,
      notes: confirmForm.notes
    })
    await load()
    ElMessage.success('人工复核已记录')
  } finally {
    confirming.value = false
  }
}

function statusType(status: string) {
  if (['DONE', 'APPROVED'].includes(status)) return 'success'
  if (['FAILED', 'REJECTED'].includes(status)) return 'danger'
  if (status === 'NEED_HUMAN_REVIEW') return 'warning'
  return 'info'
}

function riskType(risk?: string) {
  if (risk === 'CRITICAL' || risk === 'HIGH') return 'danger'
  if (risk === 'MEDIUM') return 'warning'
  if (risk === 'LOW') return 'success'
  return 'info'
}

onMounted(load)
</script>

<style scoped>
.upload-actions {
  margin: 12px 0;
}
</style>
