import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import DiscrepancyQueueView from './views/DiscrepancyQueueView.vue'
import BatchReconView from './views/BatchReconView.vue'
import EventsLedgerView from './views/EventsLedgerView.vue'
import ReportsView from './views/ReportsView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', component: DashboardView, meta: { title: '总览' } },
    { path: '/discrepancies', component: DiscrepancyQueueView, meta: { title: '财务处置队列' } },
    { path: '/batches', component: BatchReconView, meta: { title: '批次对账' } },
    { path: '/events', component: EventsLedgerView, meta: { title: '事件与账本' } },
    { path: '/reports', component: ReportsView, meta: { title: '月度报表版本' } },
  ],
})

export default router
