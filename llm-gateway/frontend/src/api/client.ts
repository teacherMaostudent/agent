import axios, { AxiosError } from 'axios'
import { ElMessage } from 'element-plus'

const authState = {
  username: localStorage.getItem('admin_username') || 'admin',
  password: localStorage.getItem('admin_password') || 'admin123',
  apiKey: localStorage.getItem('gateway_api_key') || ''
}

export const api = axios.create({
  baseURL: '/',
  timeout: 30000
})

api.interceptors.request.use((config) => {
  const token = btoa(`${authState.username}:${authState.password}`)
  config.headers.Authorization = `Basic ${token}`
  if (authState.apiKey) {
    config.headers['X-Api-Key'] = authState.apiKey
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ message?: string; error?: string }>) => {
    const message = error.response?.data?.message || error.response?.data?.error || error.message
    ElMessage.error(message)
    return Promise.reject(error)
  }
)

export function updateAuth(username: string, password: string, apiKey: string) {
  authState.username = username
  authState.password = password
  authState.apiKey = apiKey
  localStorage.setItem('admin_username', username)
  localStorage.setItem('admin_password', password)
  localStorage.setItem('gateway_api_key', apiKey)
}

export function getAuth() {
  return { ...authState }
}

export async function getOverview() {
  return (await api.get('/admin/overview')).data
}

export async function getProviders() {
  return (await api.get('/admin/providers')).data as Record<string, ProviderSummary>
}

export async function getRoutes() {
  return (await api.get('/admin/routes')).data as Record<string, RouteConfig>
}

export async function saveRoute(name: string, route: RouteConfig) {
  return (await api.put(`/admin/routes/${encodeURIComponent(name)}`, route)).data
}

export async function deleteRoute(name: string) {
  return (await api.delete(`/admin/routes/${encodeURIComponent(name)}`)).data
}

export async function probeModels() {
  return (await api.post('/admin/models/probe')).data
}

export async function getPromptTemplates() {
  return (await api.get('/admin/prompt-templates')).data as Record<string, PromptTemplate>
}

export async function getCostReport() {
  return (await api.get('/admin/reports/cost')).data
}

export async function getDailyCostReport() {
  return (await api.get('/admin/reports/cost/daily')).data
}

export async function getPerformanceReport() {
  return (await api.get('/admin/reports/performance')).data
}

export async function getEvaluation() {
  return (await api.get('/admin/eval')).data
}

export async function getEvaluationGovernance() {
  return (await api.get('/admin/eval/governance')).data
}

export async function submitFeedback(payload: unknown) {
  return (await api.post('/v1/feedback', payload)).data
}

export async function getEngineering() {
  return (await api.get('/admin/engineering')).data
}

export async function chatCompletion(payload: unknown, stream = false, requestId?: string) {
  const resolvedRequestId = requestId || crypto.randomUUID()
  const identityHeaders = {
    'X-Request-Id': resolvedRequestId,
    'X-Trace-Id': resolvedRequestId,
    'X-Agent-Id': 'llm-gateway-admin-console',
    'X-Agent-Version': '1.0.0',
    'X-Session-Id': sessionStorage.getItem('gateway-session-id') || 'admin-console',
    'X-Run-Id': resolvedRequestId,
    'X-Purpose': 'model-playground'
  }
  if (!stream) {
    return (await api.post('/v1/chat/completions', payload, {
      headers: {
        'Content-Type': 'application/json',
        'X-User-Id': 'console-demo',
        ...identityHeaders
      }
    })).data
  }
  const response = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      'X-User-Id': 'console-demo',
      ...identityHeaders,
      Authorization: `Basic ${btoa(`${authState.username}:${authState.password}`)}`,
      ...(authState.apiKey ? { 'X-Api-Key': authState.apiKey } : {})
    },
    body: JSON.stringify(payload)
  })
  if (!response.ok || !response.body) {
    throw new Error(await response.text())
  }
  return response.body
}

export async function uploadGmpDocument(file: File, businessId: string, documentType: string) {
  const form = new FormData()
  form.append('file', file)
  if (businessId) form.append('businessId', businessId)
  if (documentType) form.append('documentType', documentType)
  return (await api.post('/v1/gmp/documents/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data', 'X-User-Id': 'console-demo' }
  })).data
}

export async function startGmpReview(payload: GmpReviewRequest) {
  return (await api.post('/v1/gmp/reviews', payload, {
    headers: { 'X-User-Id': 'console-demo' }
  })).data as GmpReviewTask
}

export async function getGmpReviews() {
  return (await api.get('/admin/gmp/reviews')).data as GmpReviewTask[]
}

export async function getGmpSnapshot() {
  return (await api.get('/admin/gmp')).data
}

export async function refreshGmpReview(taskId: string) {
  return (await api.post(`/v1/gmp/reviews/${encodeURIComponent(taskId)}/refresh`)).data as GmpReviewTask
}

export async function rerunGmpReview(taskId: string) {
  return (await api.post(`/v1/gmp/reviews/${encodeURIComponent(taskId)}/rerun`)).data as GmpReviewTask
}

export async function confirmGmpReview(taskId: string, payload: GmpHumanReviewRequest) {
  return (await api.post(`/v1/gmp/reviews/${encodeURIComponent(taskId)}/confirm`, payload)).data as GmpReviewTask
}

export interface ProviderSummary {
  protocol: string
  baseUrl: string
  apiKeyConfigured: boolean
  models: string[]
}

export interface RouteConfig {
  primary: string
  fallbacks?: string[]
  weighted?: Array<{ target: string; weight: number }>
  canary?: Array<{ target: string; percent: number }>
}

export interface PromptTemplate {
  system?: string
  user?: string
}

export interface GmpReviewRequest {
  documentId?: string
  businessId?: string
  documentType: string
  content?: string
  model?: string
  checklistVersion?: string
  reviewerHint?: string
  metadata?: Record<string, unknown>
}

export interface GmpHumanReviewRequest {
  reviewer?: string
  action?: string
  finalRiskLevel?: string
  finalSummary?: string
  finalResult?: unknown
  notes?: string
}

export interface GmpReviewTask {
  taskId: string
  ragReviewId?: string
  documentId?: string
  businessId?: string
  documentType: string
  tenantId: string
  userId: string
  status: string
  riskLevel?: string
  summary?: string
  needHumanReview: boolean
  cost?: number
  latencyMs?: number
  ragResponse?: unknown
  reviewer?: string
  reviewedAt?: string
  reviewNotes?: string
  metadata?: Record<string, unknown>
  createdAt: string
  updatedAt: string
}
