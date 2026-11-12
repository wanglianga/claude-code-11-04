<template>
  <div>
    <div class="toolbar">
      <select v-model="tab" @change="load" style="width:180px">
        <option value="business">标准业务事件（L2）</option>
        <option value="raw">原始事件（L1）</option>
        <option value="ledger">账本分录</option>
        <option value="orders">订单状态投影</option>
        <option value="payments">支付状态投影</option>
      </select>
      <input v-model="filterRef" placeholder="按订单号过滤，如 ORD-005" style="width:220px"
             @keyup.enter="load" />
      <button class="primary" @click="load">查询</button>
      <div class="spacer"></div>
      <span class="muted">事件与分录只追加，无任何编辑/删除入口</span>
    </div>

    <div class="card">
      <!-- 标准业务事件 -->
      <div v-if="tab === 'business'" class="table-wrap">
        <table class="data">
          <thead><tr><th>业务键</th><th>事件</th><th>实体</th><th>订单</th><th>渠道参考</th><th>来源</th><th>业务时间</th><th>版本</th></tr></thead>
          <tbody>
            <tr v-for="e in rows" :key="e.id">
              <td class="mono" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">{{ e.business_key }}</td>
              <td><b>{{ e.event_type }}</b></td>
              <td class="mono">{{ e.entity_ref }}</td>
              <td class="mono">{{ e.order_ref || '—' }}</td>
              <td class="mono">{{ e.channel_ref || '—' }}</td>
              <td><span class="badge PROPOSED">{{ e.source }}</span></td>
              <td class="mono">{{ fmtTime(e.event_time) }}</td>
              <td>v{{ e.event_version }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 原始事件 -->
      <div v-if="tab === 'raw'" class="table-wrap">
        <table class="data">
          <thead><tr><th>来源</th><th>幂等键</th><th>主题</th><th>发生时间</th><th>接收时间</th><th>状态</th><th>备注</th></tr></thead>
          <tbody>
            <tr v-for="r in rows" :key="r.id">
              <td><span class="badge PROPOSED">{{ r.source }}</span></td>
              <td class="mono">{{ r.idempotency_key }}</td>
              <td>{{ r.topic }}</td>
              <td class="mono">{{ fmtTime(r.event_occurred_at) }}</td>
              <td class="mono muted">{{ fmtTime(r.received_at) }}</td>
              <td>{{ r.status }}</td>
              <td class="muted">{{ r.reject_reason || '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 账本分录 -->
      <div v-if="tab === 'ledger'" class="table-wrap">
        <table class="data">
          <thead>
            <tr><th>事件#</th><th>科目</th><th>方向</th>
              <th class="num">交易币</th><th class="num">结算币</th><th class="num">记账币</th>
              <th>汇率来源</th><th>归属期</th><th>原始金额</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in rows" :key="e.id">
              <td class="mono">{{ e.business_event_id }}</td>
              <td class="mono">{{ e.account }}</td>
              <td><b :class="e.direction === 'DR' ? 'pos' : ''">{{ e.direction }}</b></td>
              <td class="num">{{ fmt(e.txn_amount) }} {{ e.txn_currency }}</td>
              <td class="num">{{ fmt(e.settle_amount) }} {{ e.settle_currency }}</td>
              <td class="num"><b>{{ fmt(e.book_amount) }}</b> {{ e.book_currency }}</td>
              <td class="muted" style="font-size:11.5px">{{ e.rate_source || '—' }}</td>
              <td>{{ e.effective_month }}</td>
              <td class="num muted">{{ fmt(e.raw_amount) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 订单投影 -->
      <div v-if="tab === 'orders'" class="table-wrap">
        <table class="data">
          <thead><tr><th>订单</th><th>状态</th><th class="num">金额</th><th>币种</th><th>渠道</th><th>驱动事件#</th></tr></thead>
          <tbody>
            <tr v-for="o in rows" :key="o.order_ref">
              <td class="mono">{{ o.order_ref }}</td>
              <td><span class="badge CONFIRMED">{{ o.status }}</span></td>
              <td class="num">{{ fmt(o.amount) }}</td>
              <td>{{ o.currency }}</td><td>{{ o.channel }}</td>
              <td class="mono">{{ o.last_event_id }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 支付投影 -->
      <div v-if="tab === 'payments'" class="table-wrap">
        <table class="data">
          <thead><tr><th>支付</th><th>订单</th><th>状态</th><th class="num">授权</th><th class="num">捕获</th><th class="num">退款</th><th class="num">拒付</th></tr></thead>
          <tbody>
            <tr v-for="p in rows" :key="p.payment_ref">
              <td class="mono">{{ p.payment_ref }}</td><td class="mono">{{ p.order_ref }}</td>
              <td><span class="badge CONFIRMED">{{ p.state }}</span></td>
              <td class="num">{{ fmt(p.authorized_amount) }}</td>
              <td class="num">{{ fmt(p.captured_amount) }}</td>
              <td class="num">{{ fmt(p.refunded_amount) }}</td>
              <td class="num">{{ fmt(p.chargeback_amount) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!rows.length" class="empty">暂无数据</div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const tab = ref('business')
const filterRef = ref('')
const rows = ref([])

const fmt = (v) => Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 })
const fmtTime = (t) => (t ? new Date(t).toLocaleString('zh-CN') : '')

async function load() {
  const q = filterRef.value ? { order_ref: filterRef.value } : {}
  switch (tab.value) {
    case 'business': rows.value = await api.businessEvents(q); break
    case 'raw': rows.value = await api.rawEvents(); break
    case 'ledger': rows.value = await api.ledgerEntries(q); break
    case 'orders': rows.value = await api.orderStates(); break
    case 'payments': rows.value = await api.paymentStates(); break
  }
}
onMounted(load)
</script>
