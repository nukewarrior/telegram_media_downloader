import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './styles.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/setup', component: () => import('./views/SetupView.vue') },
    { path: '/tasks', component: () => import('./views/TasksView.vue') },
    { path: '/tasks/new', component: () => import('./views/NewTaskView.vue') },
    { path: '/tasks/:id', component: () => import('./views/TaskDetailView.vue') },
    { path: '/sources', component: () => import('./views/SourcesView.vue') },
    { path: '/archives', component: () => import('./views/ArchivesView.vue') },
    { path: '/settings', component: () => import('./views/SettingsView.vue') },
  ],
})

createApp(App).use(router).mount('#app')
