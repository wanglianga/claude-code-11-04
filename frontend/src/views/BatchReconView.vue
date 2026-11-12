<template>
  <div>
    <div class="toolbar">
      <button class="primary" @click="runAll">运行自动对账（全部 OPEN 批次）</button>
      <button @click="load">刷新</button>
      <div class="spacer"></div>
      <span class="muted">自动任务只生成 PROPOSED 匹配与差异提示</span>
    </div>

    <div class="card">
      <div class="card-title">结算批次（修订产生新版本，旧版本保留）</div>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>结算单号</th><th>版本</th><th>渠道/商户</th><th>周期</th>
              <th class="num">总额</th><th class="num">手续费</th><th class="num">净额</th>
              <th>状态</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in batches" :key="b.id">
              <td class="mono">{{ b.batch_ref }}</td>
              <td>v{{ b.version }}</td>
              <td>{{ b.channel }} / {{ b.merchant_id }}</td>
              <td class="mono">{{ b.period_start.slice(0, 10) }} ~ {{ b.period_end.slice(0, 10) }}</td>
              <td class="num">{{ fmt(b.gross_amount) }}</td>
              <td class="num">{{ fmt(b.fee_amount) }}</td>
              <td class="num"><b>{{ fmt(b.net_amount) }}</b></td>
              <td><span class="badge" :class="b.status">{{ b.status }}</span></td>
              <td class="pill-row">
                <button class="sm" @click="openBatch(b)">详情</button>
                <button v-if="b.status === 'OPEN'" class="sm" @click="reconcile(b)">对账</button>
                <button v-if="b.status === 'OPEN'" class="sm danger" @click="close(b)">关闭</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="detail" class="card section-gap">
      <div class="card-title">
        批次 {{ detail.batch.batch_ref }} v{{ detail.batch.version }} · 结算行与两层匹配
      </div>
      <div class="grid cols-2">
        <div>
          <div class="soft" style="font-weight:600;margin-bottom:8px">LEVEL 1：订单事件 → 结算行</div>
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>行</th><th>订单</th><th>类型</th><th class="num">净额</th><th>匹配</th></tr></thead>
              <tbody>
                <tr v-for="l in detail.lines" :key="l.id">
                  <td class="mono">{{ l.line_ref }}</td>
                  <td class="mono">{{ l.order_ref || '—' }}</td>
                  <td>{{ l.line_type }}</td>
                  <td class="num">{{ fmt(l.net_amount) }}</td>
                  <td>
                    <span :class="l.matched ? 'pos' : 'neg'">
                      {{ l.matched ? '✓ 已建议' : '✗ 未匹配' }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <div class="soft" style="font-weight:600;margin-bottom:8px">LEVEL 2：结算单 → 银行流水（支持合并/拆分）</div>
          <div class="table-wrap">
            <table class="data">
              <thead><tr><th>银行流水</th><th class="num">分配金额</th><th>状态</th><th></th></tr></thead>
              <tbody>
                <tr v-for="m in level2" :key="m.id">
                  <td class="mono">{{ bankRef(m.bank_transaction_id) }}</td>
                  <td class="num">{{ fmt(m.allocated_amount) }} {{ m.currency }}</td>
                  <td><span class="badge" :class="m.status">{{ m.status }}</span></td>
                  <td>
                    <button v-if="m.status === 'PROPOSED'" class="sm"
                            @click="confirm(m.id)">人工确认</button>
                  </td>
                </tr>
                <tr v-if="!level2.length"><td colspan="4" class="empty">暂无 LEVEL2 匹配</td></tr>
              </tbody>
            </table>
          </div>
          <div class="soft" style="font-weight:600;margin:14px 0 8px">银行流水</div>
          <table class="data">
            <thead><tr><th>流水号</th><th>入账日</th><th class="num">金额</th><th>摘要</th></tr></thead>
            <tbody>
              <tr v-for="bk in banks" :key="bk.id">
                <td class="mono">{{ bk.bank_ref }}</td>
                <td>{{ bk.booked_at.slice(0, 10) }}</td>
                <td class="num">{{ fmt(bk.amount) }} {{ bk.currency }}</td>
                <td class="muted">{{ bk.memo }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div v-if="snapshot" class="card section-gap">
      <div class="card-title">关闭快照（从只追加事件/分录还原，历史不可静默篡改）</div>
      <pre class="json">{{ JSON.stringify(snapshot, null, 2) }}</pre>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const batches = ref([])
const detail = ref(null)
const level2 = ref([])
const banks = ref([])
const snapshot = ref(null)

const fmt = (v) => Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 })
const bankRef = (id) => bankMap.value[id] || `#${id}`
const bankMap = ref({})

async function load() {
  batches.value = await api.batches()
  banks.value = await api.bankTxns()
  bankMap.value = Object.fromEntries(banks.value.map((b) => [b.id, b.bank_ref]))
}
async function openBatch(b) {
  detail.value = await api.batch(b.id)
  const all = await api.matches({ batch_id: b.id })
  level2.value = all.filter((m) => m.match_level === 'SETTLEMENT_TO_BANK')
  snapshot.value = await api.snapshot(b.id)
}
async function reconcile(b) {
  try {
    const r = await api.reconcile(b.id)
    alert(`对账完成：未关闭差异 ${r.discrepancies_open} 条\n` + JSON.stringify(r.by_kind))
    await load()
    await openBatch(b)
  } catch (e) { alert(e.message) }
}
async function runAll() {
  try {
    const r = await api.runAllReconcile()
    alert(`自动对账完成，扫描 ${r.length} 个 OPEN 批次（仅建议，未核销任何差异）`)
    await load()
  } catch (e) { alert(e.message) }
}
async function confirm(matchId) {
  await api.confirmMatch(matchId, 'finance.chen')
  if (detail.value) await openBatch(detail.value.batch)
}
async function close(b) {
  if (!confirm(`关闭批次 ${b.batch_ref} v${b.version}？前提：所有差异已人工关闭。`)) return
  try {
    const r = await api.closeBatch(b.id, 'finance.chen')
    alert(`已关闭，生成报表 v${r.report_version}`)
    await load()
  } catch (e) { alert(e.message) }
}

onMounted(load)
</script>
