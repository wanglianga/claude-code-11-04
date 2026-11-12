<template>
  <div>
    <div class="grid cols-4">
      <div class="card">
        <div class="card-title">账本守恒（记账币）</div>
        <div class="stat-value" :class="balances.balanced ? 'pos' : 'neg'">
          {{ balances.balanced ? '平衡' : '不平衡' }}
        </div>
        <div class="stat-sub">
          借 {{ fmt(balances.total_debit) }} / 贷 {{ fmt(balances.total_credit) }}
        </div>
      </div>
      <div class="card">
        <div class="card-title">待处置差异</div>
        <div class="stat-value neg">{{ openCount }}</div>
        <div class="stat-sub">自动任务仅提示，核销须人工</div>
      </div>
      <div class="card">
        <div class="card-title">结算批次</div>
        <div class="stat-value">{{ batches.length }}</div>
        <div class="stat-sub">
          OPEN {{ openBatches }} · CLOSED {{ batches.length - openBatches }}
        </div>
      </div>
      <div class="card">
        <div class="card-title">报表当前版本</div>
        <div class="stat-value">{{ currentVersions }}</div>
        <div class="stat-sub">历史版本只追加不覆盖</div>
      </div>
    </div>

    <div class="grid cols-2 section-gap">
      <div class="card">
        <div class="card-title">差异归因分布（全部状态）</div>
        <div v-if="kindRows.length" class="pill-row" style="gap:12px">
          <div v-for="r in kindRows" :key="r.kind" style="min-width:120px">
            <span class="badge" :class="r.kind">{{ kindLabel(r.kind) }}</span>
            <div style="font-size:22px;font-weight:700;margin-top:8px">{{ r.count }}</div>
            <div class="muted" style="font-size:12px">{{ kindHint(r.kind) }}</div>
          </div>
        </div>
        <div v-else class="empty">暂无差异</div>
      </div>
      <div class="card">
        <div class="card-title">支付状态投影（按业务时间重放）</div>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>支付</th><th>订单</th><th>状态</th><th class="num">捕获</th><th class="num">退款</th><th class="num">拒付</th></tr></thead>
            <tbody>
              <tr v-for="p in payments" :key="p.payment_ref">
                <td class="mono">{{ p.payment_ref }}</td>
                <td class="mono">{{ p.order_ref }}</td>
                <td><span class="badge PROPOSED">{{ p.state }}</span></td>
                <td class="num">{{ p.captured_amount }}</td>
                <td class="num">{{ p.refunded_amount }}</td>
                <td class="num">{{ p.chargeback_amount }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card section-gap">
      <div class="card-title">科目余额（记账币 CNY）</div>
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>科目</th><th class="num">借方</th><th class="num">贷方</th></tr></thead>
          <tbody>
            <tr v-for="(v, acc) in balances.accounts" :key="acc">
              <td class="mono">{{ acc }}</td>
              <td class="num">{{ fmt(v.DR) }}</td>
              <td class="num">{{ fmt(v.CR) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'

const balances = ref({ accounts: {}, total_debit: 0, total_credit: 0, balanced: true })
const allDiscs = ref([])
const batches = ref([])
const reports = ref([])
const payments = ref([])

const openCount = computed(() => allDiscs.value.filter((d) => d.status !== 'CLOSED').length)
const openBatches = computed(() => batches.value.filter((b) => b.status === 'OPEN').length)
const currentVersions = computed(
  () => reports.value.reduce((s, r) => s + (r.current_version > 0 ? 1 : 0), 0),
)
const kindRows = computed(() => {
  const map = {}
  for (const d of allDiscs.value) map[d.kind] = (map[d.kind] || 0) + 1
  return Object.entries(map).map(([kind, count]) => ({ kind, count }))
})

const fmt = (v) => Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 })
const kindLabel = (k) => ({
  AMOUNT: '金额差', FEE: '手续费差', FX: '汇率差', TIMING: '时间差', UNKNOWN: '未知差',
}[k] || k)
const kindHint = (k) => ({
  AMOUNT: '订单与结算本金不一致',
  FEE: '手续费后补/比例差异',
  FX: '渠道汇率与入账汇率差异',
  TIMING: '银行入账延迟',
  UNKNOWN: '禁止自动核销，必须人工调账',
}[k] || '')

onMounted(async () => {
  balances.value = await api.ledgerBalances()
  allDiscs.value = await api.discrepancies({})
  batches.value = await api.batches()
  reports.value = await api.reports()
  payments.value = await api.paymentStates()
})
</script>
