<template>
  <div class="grid cols-2">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>Chat Playground</h3>
          <p>调用 `/v1/chat/completions`</p>
        </div>
        <el-switch v-model="stream" active-text="流式" inactive-text="普通" />
      </div>
      <el-form label-width="92px">
        <el-form-item label="模型">
          <el-input v-model="model" />
        </el-form-item>
        <el-form-item label="System">
          <el-input v-model="system" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="User">
          <el-input v-model="prompt" type="textarea" :rows="5" />
        </el-form-item>
        <el-form-item label="温度">
          <el-slider v-model="temperature" :min="0" :max="1" :step="0.1" />
        </el-form-item>
        <el-form-item>
          <el-space>
            <el-button type="primary" :loading="running" @click="send">发送</el-button>
            <el-button @click="fillGatewayPrompt">示例</el-button>
          </el-space>
        </el-form-item>
      </el-form>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>模型响应</h3>
        <el-tag v-if="latency">{{ latency }} ms</el-tag>
      </div>
      <div class="answer">{{ answer || '等待请求...' }}</div>
      <el-space v-if="requestId && answer" class="feedback-actions">
        <el-tooltip content="回答有帮助" placement="top">
          <el-button circle :icon="ArrowUpBold" @click="sendFeedback('UP')" />
        </el-tooltip>
        <el-tooltip content="回答不符合预期" placement="top">
          <el-button circle :icon="ArrowDownBold" @click="openDownvote" />
        </el-tooltip>
        <span class="request-id">Request {{ requestId }}</span>
      </el-space>
      <JsonBlock v-if="raw" :value="raw" />
    </section>
  </div>

  <el-dialog v-model="feedbackDialog" title="提交问题反馈" width="520px">
    <el-form label-width="88px">
      <el-form-item label="问题原因">
        <el-input v-model="feedbackReason" type="textarea" :rows="3" placeholder="例如：事实错误、遗漏关键条件、格式不符合要求" />
      </el-form-item>
      <el-form-item label="期望答案">
        <el-input v-model="expectedAnswer" type="textarea" :rows="4" placeholder="可选，将作为 Golden 候选的重要依据" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="feedbackDialog = false">取消</el-button>
      <el-button type="primary" :loading="feedbackSubmitting" @click="sendFeedback('DOWN')">提交</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ArrowDownBold, ArrowUpBold } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { chatCompletion, submitFeedback } from '../api/client'
import JsonBlock from '../components/JsonBlock.vue'

const model = ref('deepseek-v4-flash')
const system = ref('You are a concise AI engineering assistant.')
const prompt = ref('解释一下 LLM Gateway 的价值。')
const temperature = ref(0.2)
const stream = ref(false)
const running = ref(false)
const answer = ref('')
const raw = ref<unknown>(null)
const latency = ref(0)
const requestId = ref('')
const feedbackDialog = ref(false)
const feedbackReason = ref('')
const expectedAnswer = ref('')
const feedbackSubmitting = ref(false)

function fillGatewayPrompt() {
  prompt.value = '用面试回答的方式，说明 LLM Gateway 的模型路由、fallback、成本统计和 Prompt 治理。'
}

async function send() {
  running.value = true
  answer.value = ''
  raw.value = null
  requestId.value = crypto.randomUUID()
  const started = performance.now()
  const payload = {
    model: model.value,
    messages: [
      { role: 'system', content: system.value },
      { role: 'user', content: prompt.value }
    ],
    temperature: temperature.value,
    stream: stream.value
  }
  try {
    if (!stream.value) {
      const data = await chatCompletion(payload, false, requestId.value)
      raw.value = data
      answer.value = data?.choices?.[0]?.message?.content || JSON.stringify(data)
    } else {
      const body = await chatCompletion(payload, true, requestId.value) as ReadableStream<Uint8Array>
      const reader = body.getReader()
      const decoder = new TextDecoder()
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        answer.value += decoder.decode(value, { stream: true })
      }
    }
  } finally {
    latency.value = Math.round(performance.now() - started)
    running.value = false
  }
}

function openDownvote() {
  feedbackDialog.value = true
}

async function sendFeedback(rating: 'UP' | 'DOWN') {
  feedbackSubmitting.value = true
  try {
    await submitFeedback({
      requestId: requestId.value,
      rating,
      reason: rating === 'DOWN' ? feedbackReason.value : 'helpful',
      expectedAnswer: rating === 'DOWN' ? expectedAnswer.value : '',
      userId: 'console-demo',
      metadata: { source: 'admin-playground', model: model.value }
    })
    feedbackDialog.value = false
    ElMessage.success(rating === 'UP' ? '感谢反馈' : '已进入自动评估流程')
  } finally {
    feedbackSubmitting.value = false
  }
}

</script>

<style scoped>
.answer {
  min-height: 220px;
  white-space: pre-wrap;
  line-height: 1.7;
  border: 1px solid #e6eaf0;
  border-radius: 8px;
  padding: 14px;
  margin-bottom: 14px;
  background: #fbfcff;
}

.feedback-actions {
  margin-bottom: 14px;
}

.request-id {
  color: #7a8494;
  font-size: 12px;
}
</style>
