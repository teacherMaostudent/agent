import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'
import Models from './views/Models.vue'
import Routing from './views/Routing.vue'
import Prompts from './views/Prompts.vue'
import Playground from './views/Playground.vue'
import Evaluation from './views/Evaluation.vue'
import Logs from './views/Logs.vue'
import Settings from './views/Settings.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', component: Dashboard },
    { path: '/models', component: Models },
    { path: '/routing', component: Routing },
    { path: '/prompts', component: Prompts },
    { path: '/playground', component: Playground },
    { path: '/evaluation', component: Evaluation },
    { path: '/logs', component: Logs },
    { path: '/settings', component: Settings }
  ]
})
