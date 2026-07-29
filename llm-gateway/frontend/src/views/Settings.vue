<template>
  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>连接设置</h3>
        <p>保存到浏览器本地，用于 Admin Basic Auth 和网关 API Key</p>
      </div>
    </div>
    <el-form label-width="120px" style="max-width: 640px">
      <el-form-item label="Admin 用户">
        <el-input v-model="username" />
      </el-form-item>
      <el-form-item label="Admin 密码">
        <el-input v-model="password" type="password" show-password />
      </el-form-item>
      <el-form-item label="X-Api-Key">
        <el-input v-model="apiKey" type="password" show-password placeholder="可选，业务 API Key" />
      </el-form-item>
      <el-form-item>
        <el-space>
          <el-button type="primary" @click="save">保存</el-button>
          <el-button @click="test">测试连接</el-button>
        </el-space>
      </el-form-item>
    </el-form>
    <JsonBlock :value="result" />
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getAuth, getOverview, updateAuth } from '../api/client'
import JsonBlock from '../components/JsonBlock.vue'

const auth = getAuth()
const username = ref(auth.username)
const password = ref(auth.password)
const apiKey = ref(auth.apiKey)
const result = ref({})

function save() {
  updateAuth(username.value, password.value, apiKey.value)
  ElMessage.success('已保存')
}

async function test() {
  save()
  result.value = await getOverview()
}
</script>
