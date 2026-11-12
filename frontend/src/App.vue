<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">RC</div>
        <div>
          <div class="brand-title">清结算差异中心</div>
          <div class="brand-sub">Reconciliation Center</div>
        </div>
      </div>
      <nav class="nav">
        <RouterLink v-for="m in menu" :key="m.path" :to="m.path" class="nav-item">
          <span class="nav-icon">{{ m.icon }}</span>
          <span>{{ m.label }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar-note">
        <div>四层架构 · 只追加账本</div>
        <div>自动任务只提示，核销必须人工</div>
      </div>
    </aside>
    <main class="main">
      <header class="topbar">
        <h1>{{ $route.meta.title || '差异处理中心' }}</h1>
        <div class="topbar-right">
          <span class="health-dot" :class="{ down: !healthy }"></span>
          <span class="health-text">{{ healthy ? '后端在线' : '后端不可达' }}</span>
        </div>
      </header>
      <section class="content">
        <RouterView />
      </section>
    </main>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import axios from 'axios'

const menu = [
  { path: '/dashboard', label: '总览', icon: '▦' },
  { path: '/discrepancies', label: '财务处置队列', icon: '⚠' },
  { path: '/batches', label: '批次对账', icon: '⛓' },
  { path: '/events', label: '事件与账本', icon: '≣' },
  { path: '/reports', label: '月度报表版本', icon: '◷' },
]

const healthy = ref(true)
const check = async () => {
  try {
    await axios.get('/health', { timeout: 5000 })
    healthy.value = true
  } catch {
    healthy.value = false
  }
}
onMounted(() => {
  check()
  setInterval(check, 15000)
})
</script>
