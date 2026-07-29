<template>
  <el-container class="shell">
    <el-aside width="252px" class="sidebar">
      <div class="brand">
        <div class="brand-mark">LG</div>
        <div>
          <strong>LLM Gateway</strong>
          <span>Ops Console</span>
        </div>
      </div>
      <el-menu router :default-active="$route.path" class="nav" background-color="transparent">
        <el-menu-item index="/dashboard"><el-icon><DataLine /></el-icon><span>Dashboard</span></el-menu-item>
        <el-menu-item index="/models"><el-icon><Cpu /></el-icon><span>模型治理</span></el-menu-item>
        <el-menu-item index="/routing"><el-icon><Share /></el-icon><span>路由策略</span></el-menu-item>
        <el-menu-item index="/prompts"><el-icon><EditPen /></el-icon><span>Prompt 管理</span></el-menu-item>
        <el-menu-item index="/playground"><el-icon><ChatDotRound /></el-icon><span>Playground</span></el-menu-item>
        <el-menu-item index="/evaluation"><el-icon><TrendCharts /></el-icon><span>评估中心</span></el-menu-item>
        <el-menu-item index="/logs"><el-icon><Tickets /></el-icon><span>调用观测</span></el-menu-item>
        <el-menu-item index="/settings"><el-icon><Setting /></el-icon><span>连接设置</span></el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <div>
          <h1>{{ title }}</h1>
          <p>{{ subtitle }}</p>
        </div>
        <el-space>
          <el-tag effect="plain">localhost:8080</el-tag>
          <el-button :icon="Refresh" circle @click="reload" />
        </el-space>
      </el-header>
      <el-main class="content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  ChatDotRound,
  Cpu,
  DataLine,
  EditPen,
  Refresh,
  Setting,
  Share,
  Tickets,
  TrendCharts
} from '@element-plus/icons-vue'

const route = useRoute()

const meta = computed(() => {
  const map: Record<string, [string, string]> = {
    '/dashboard': ['运行总览', '模型调用、成本、健康状态和性能指标'],
    '/models': ['模型治理', 'Provider、模型列表、健康探测和协议适配'],
    '/routing': ['路由策略', 'primary、weighted、canary、fallback 热更新'],
    '/prompts': ['Prompt 管理', '模板、变量、bad case 和输出契约'],
    '/playground': ['Playground', 'OpenAI-compatible 普通与 SSE 模型调用'],
    '/evaluation': ['评估中心', 'Golden Dataset、Ragas 风格指标和回归记录'],
    '/logs': ['调用观测', '成本报表、性能报表、trace 和工程治理'],
    '/settings': ['连接设置', 'Admin Basic Auth 与网关 API Key']
  }
  return map[route.path] || map['/dashboard']
})

const title = computed(() => meta.value[0])
const subtitle = computed(() => meta.value[1])

function reload() {
  window.location.reload()
}
</script>
