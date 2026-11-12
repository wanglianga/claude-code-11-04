<template>
  <div>
    <div class="toolbar">
      <select v-model="filters.status" @change="load" style="width:150px">
        <option value="">全部状态</option>
        <option>OPEN</option><option>CLAIMED</option>
        <option>RESOLVED</option><option>CLOSED</option>
      </select>
      <select v-model="filters.kind" @change="load" style="width:150px">
        <option value="">全部类型</option>
        <option value="AMOUNT">金额差</option><option value="FEE">手续费差</option>
        <option value="FX">汇率差</option><option value="TIMING">时间差</option>
        <option value="UNKNOWN">未知差</option>
      </select>
      <button class="primary" @click="load">刷新</button>
      <div class="spacer"></div>
      <span class="muted">共 {{ rows.length }} 条</span>
    </div>

    <div class="card">
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>#</th><th>类型</th><th>事项</th><th>订单</th>
              <th class="num">预期</th><th class="num">实际</th><th class="num">差额</th>
              <th>币种</th><th>状态</th><th>处理人</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in rows" :key="d.id">
              <td class="mono muted">{{ d.id }}</td>
              <td><span class="badge" :class="d.kind">{{ kindLabel(d.kind) }}</span></td>
              <td>{{ d.title }}</td>
              <td class="mono">{{ d.order_ref || '—' }}</td>
              <td class="num">{{ fmt(d.expected_amount) }}</td>
              <td class="num">{{ fmt(d.actual_amount) }}</td>
              <td class="num" :class="d.diff_amount > 0 ? 'pos' : d.diff_amount < 0 ? 'neg' : ''">
                {{ fmt(d.diff_amount) }}
              </td>
              <td>{{ d.currency }}</td>
              <td><span class="badge" :class="d.status">{{ statusLabel(d.status) }}</span></td>
              <td>{{ d.assignee || '—' }}</td>
              <td><button class="sm" @click="openDetail(d)">处置 / 详情</button></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!rows.length" class="empty">没有符合条件的差异</div>
      </div>
    </div>

    <!-- 处置弹窗 -->
    <div v-if="current" class="modal-mask" @click.self="current = null">
      <div class="modal">
        <h3>
          差异 #{{ current.id }}
          <span class="badge" :class="current.kind" style="margin-left:8px">{{ kindLabel(current.kind) }}</span>
          <span class="badge" :class="current.status" style="margin-left:6px">{{ statusLabel(current.status) }}</span>
        </h3>
        <p class="soft" style="margin:0 0 6px">{{ current.title }}</p>
        <p class="muted" style="margin:0 0 14px;font-size:12.5px">
          订单 {{ current.order_ref || '—' }} · 渠道参考 {{ current.channel_ref || '—' }} ·
          差额 {{ fmt(current.diff_amount) }} {{ current.currency }}
          <span v-if="current.kind === 'UNKNOWN'" class="neg">（未知差禁止直接关闭，须先过账人工调账）</span>
        </p>

        <div v-if="current.status !== 'CLOSED'" class="pill-row" style="margin-bottom:16px">
          <button class="sm" @click="mode = 'claim'">认领</button>
          <button class="sm" @click="mode = 'attach'">附加材料</button>
          <button class="sm" @click="mode = 'suggest'">调账建议</button>
          <button class="sm primary" :disabled="!hasSuggestion && current.kind === 'UNKNOWN'"
                  @click="mode = 'post'">过账调账</button>
          <button class="sm danger" @click="mode = 'close'">关闭确认</button>
        </div>

        <div v-if="mode === 'claim'">
          <label class="field"><span>处理人</span><input v-model="form.operator" /></label>
          <label class="field"><span>备注</span><input v-model="form.comment" /></label>
          <button class="primary" @click="doAction(() => api.claim(current.id, form.operator, form.comment))">提交认领</button>
        </div>
        <div v-if="mode === 'attach'">
          <label class="field"><span>处理人</span><input v-model="form.operator" /></label>
          <label class="field"><span>材料（JSON，如凭证编号/文件名）</span>
            <textarea rows="3" v-model="form.attachmentJson" placeholder='{"voucher":"V-202610-001"}'></textarea>
          </label>
          <button class="primary" @click="doAttach">提交材料</button>
        </div>
        <div v-if="mode === 'suggest'">
          <label class="field"><span>处理人</span><input v-model="form.operator" /></label>
          <label class="field"><span>调账金额（记账币，带方向）</span>
            <input v-model="form.adjust_amount" type="number" step="0.01" />
          </label>
          <label class="field"><span>科目</span>
            <select v-model="form.account">
              <option>MANUAL_ADJUSTMENT</option><option>PAYMENT_FEE</option>
              <option>FX_GAIN_LOSS</option><option>ROUNDING</option><option>CHARGEBACK</option>
            </select>
          </label>
          <label class="field"><span>说明</span><input v-model="form.comment" /></label>
          <button class="primary" @click="doAction(() => api.suggest(current.id, form.operator, form.adjust_amount, form.account, form.comment))">保存建议</button>
        </div>
        <div v-if="mode === 'post'">
          <p class="soft" style="font-size:12.5px">将生成独立的 MANUAL_ADJUSTMENT 业务事件与平衡分录，全程可追溯。</p>
          <label class="field"><span>处理人</span><input v-model="form.operator" /></label>
          <button class="primary" @click="doAction(() => api.postAdjust(current.id, form.operator, form.comment))">确认过账</button>
        </div>
        <div v-if="mode === 'close'">
          <label class="field"><span>处理人</span><input v-model="form.operator" /></label>
          <label class="field"><span>关闭说明</span><input v-model="form.comment" /></label>
          <button class="primary danger" @click="doAction(() => api.closeDisc(current.id, form.operator, form.comment))">关闭差异</button>
        </div>

        <div class="section-gap">
          <div class="card-title">处置轨迹（只追加）</div>
          <ul class="timeline">
            <li v-for="a in actions" :key="a.id">
              <div><b>{{ actionLabel(a.action_type) }}</b> · {{ a.operator }}</div>
              <div v-if="a.comment" class="soft" style="font-size:12.5px">{{ a.comment }}</div>
              <div v-if="a.attachment"><pre class="json">{{ JSON.stringify(a.attachment, null, 2) }}</pre></div>
              <div class="t-meta">{{ fmtTime(a.created_at) }}</div>
            </li>
            <li v-if="!actions.length" class="muted">暂无处置记录</li>
          </ul>
        </div>
        <div class="modal-actions"><button @click="current = null">关闭窗口</button></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import api from '../api'

const rows = ref([])
const current = ref(null)
const actions = ref([])
const mode = ref('')
const filters = reactive({ status: '', kind: '' })
const form = reactive({
  operator: 'finance.chen', comment: '', adjust_amount: 0,
  account: 'MANUAL_ADJUSTMENT', attachmentJson: '{"voucher":""}',
})

const kindLabel = (k) => ({
  AMOUNT: '金额差', FEE: '手续费差', FX: '汇率差', TIMING: '时间差', UNKNOWN: '未知差',
}[k] || k)
const statusLabel = (s) => ({
  OPEN: '待处理', CLAIMED: '已认领', RESOLVED: '已调账', CLOSED: '已关闭',
}[s] || s)
const actionLabel = (a) => ({
  CLAIM: '认领', ATTACH: '附加材料', ADJUST_SUGGEST: '调账建议',
  ADJUST_POST: '调账过账', CLOSE: '关闭', REOPEN: '重开',
}[a] || a)
const fmt = (v) => Number(v || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2 })
const fmtTime = (t) => (t ? new Date(t).toLocaleString('zh-CN') : '')

const hasSuggestion = ref(false)

async function load() {
  rows.value = await api.discrepancies({ ...filters })
}
async function openDetail(d) {
  current.value = d
  mode.value = ''
  const detail = await api.discrepancy(d.id)
  actions.value = detail.actions
  hasSuggestion.value = actions.value.some((a) => a.action_type === 'ADJUST_SUGGEST')
}
async function doAction(fn) {
  try {
    await fn()
    toast('操作成功', true)
    await load()
    if (current.value) await openDetail(rows.value.find((r) => r.id === current.value.id) || current.value)
  } catch (e) { toast(e.message, false) }
}
async function doAttach() {
  let attachment
  try { attachment = JSON.parse(form.attachmentJson) }
  catch { return toast('材料 JSON 格式错误', false) }
  await doAction(() => api.attach(current.value.id, form.operator, attachment, form.comment))
}

function toast(msg, ok) {
  const el = document.createElement('div')
  el.className = `toast ${ok ? 'ok' : 'err'}`
  el.textContent = msg
  document.body.appendChild(el)
  setTimeout(() => el.remove(), 3200)
}

onMounted(load)
</script>
