<template>
  <div>
    <div class="toolbar">
      <button @click="load">刷新</button>
      <button @click="regenerate">重算已关闭报表（补单后生成新版本）</button>
      <div class="spacer"></div>
      <span class="muted">关闭后更正只追加新版本，旧快照 SUPERSEDED 且原样保留</span>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <div class="card-title">月度报表</div>
        <div class="table-wrap">
          <table class="data">
            <thead><tr><th>渠道</th><th>商户</th><th>月份</th><th>币种</th><th>状态</th><th>当前版本</th></tr></thead>
            <tbody>
              <tr v-for="r in reports" :key="r.id" style="cursor:pointer"
                  :style="selected === r.id ? 'background:var(--primary-soft)' : ''"
                  @click="open(r.id)">
                <td>{{ r.channel }}</td><td>{{ r.merchant_id }}</td>
                <td class="mono">{{ r.report_month }}</td><td>{{ r.currency }}</td>
                <td><span class="badge" :class="r.status">{{ r.status }}</span></td>
                <td><b>v{{ r.current_version }}</b></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card" v-if="versions.length">
        <div class="card-title">版本链与不可变快照</div>
        <div v-for="v in versions" :key="v.id" style="margin-bottom:14px">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
            <b>v{{ v.version }}</b>
            <span class="badge" :class="v.version_status">{{ v.version_status }}</span>
            <span class="muted" style="font-size:12px">{{ fmtTime(v.created_at) }}</span>
          </div>
          <div class="soft" style="font-size:12.5px;margin-bottom:6px">{{ v.reason }}</div>
          <pre class="json">{{ JSON.stringify(v.snapshot, null, 2) }}</pre>
        </div>
      </div>
      <div class="card" v-else>
        <div class="empty">选择左侧报表查看版本链</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const reports = ref([])
const versions = ref([])
const selected = ref(null)

const fmtTime = (t) => (t ? new Date(t).toLocaleString('zh-CN') : '')

async function load() {
  reports.value = await api.reports()
  if (selected.value) await open(selected.value)
}
async function open(id) {
  selected.value = id
  versions.value = await api.reportVersions(id)
}
async function regenerate() {
  try {
    const r = await api.regenerateReports()
    alert(`生成新版本 ${r.new_versions.length} 个（内容无变化则复用当前版本）`)
    await load()
  } catch (e) { alert(e.message) }
}
onMounted(load)
</script>
