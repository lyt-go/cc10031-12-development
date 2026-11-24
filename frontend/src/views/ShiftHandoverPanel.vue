<template>
  <section class="handover-panel" data-testid="handover-panel">
    <header class="panel-head">
      <div>
        <h3>终端交接表</h3>
        <p class="page-desc">
          同一原始采样时刻的两份证据都会留档；每次实收只采用一个观测，签认后采用校准缓存，未签认时终端原件为准。
        </p>
      </div>
      <label class="role-picker">
        <span>当前角色</span>
        <select v-model="role" data-testid="role-select" @change="reloadSheet">
          <option v-for="item in roles" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="panel-error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="panel-notice">{{ notice }}</p>

    <div class="handover-toolbar">
      <label>
        <span>交接记录</span>
        <select v-model.number="selectedId" data-testid="handover-select" @change="reloadSheet">
          <option v-for="row in entries" :key="String(row.id)" :value="Number(row.id)">
            {{ row['交接编号'] }}（{{ row.status }}）
          </option>
        </select>
      </label>
      <button class="btn" data-testid="load-sheet" type="button" @click="reloadSheet">打开交接表</button>
      <button class="btn" data-testid="load-heartbeat" type="button" @click="loadSummary">查看心跳汇总</button>
      <span class="role-hint">权限：{{ sheet?.role_label ?? '—' }}<template v-if="sheet?.permissions['只读']">（只读）</template></span>
    </div>

    <template v-if="sheet">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">采用温度（交班点 07:59）</span>
          <strong class="stat-value" data-testid="adopted-temp">{{ sheet.reconciliation['采用温度'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">旧班实收 / 缺点</span>
          <strong class="stat-value" data-testid="old-shift">
            {{ sheet.reconciliation['旧班实收'] }} / {{ sheet.reconciliation['旧班缺点'] }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">新班实收 / 缺点</span>
          <strong class="stat-value" data-testid="new-shift">
            {{ sheet.reconciliation['新班实收'] }} / {{ sheet.reconciliation['新班缺点'] }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">缺点合计</span>
          <strong class="stat-value" data-testid="defect-total">{{ sheet.reconciliation['缺点合计'] }}</strong>
        </article>
      </div>

      <h4>同一采样时刻的两份依据</h4>
      <table class="data-table" data-testid="dual-evidence">
        <thead>
          <tr>
            <th>采样时刻</th><th>归属班次</th><th>采用值</th><th>采用来源</th>
            <th>采用依据</th><th>未采用值</th><th>待计量签认</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in sheet.reconciliation['同一时刻多份证据']" :key="item['采样时刻']">
            <td>{{ item['采样时刻'] }}</td>
            <td>{{ shiftOf(item['采样时刻']) }}</td>
            <td>{{ item['采用数值'] }}</td>
            <td>{{ item['采用来源'] }}</td>
            <td>{{ item['采用依据'] }}</td>
            <td>
              {{ item['未采用数值'].join('、') }}
              <span class="muted">（{{ item['未采用来源'].join('、') }}）</span>
            </td>
            <td>
              <span v-if="item['待计量签认'].length">{{ item['待计量签认'].join('、') }}</span>
              <span v-else>—</span>
            </td>
          </tr>
          <tr v-if="!sheet.reconciliation['同一时刻多份证据'].length">
            <td colspan="7" class="empty-state">该交接没有同一时刻的多份证据</td>
          </tr>
        </tbody>
      </table>

      <h4>交叠段核查（07:55–08:05）</h4>
      <table class="data-table">
        <thead>
          <tr><th>采样时刻</th><th>采用值</th><th>采用证据</th><th>采用依据</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in sheet.reconciliation['交叠段采用']" :key="String(item['采样时刻'])">
            <td>{{ item['采样时刻'] }}</td>
            <td>{{ item['采用数值'] }}</td>
            <td>{{ item['采用证据'] }}</td>
            <td>{{ item['采用依据'] }}</td>
          </tr>
        </tbody>
      </table>

      <h4>缺点区段与补采缺口</h4>
      <table class="data-table" data-testid="defect-ranges">
        <thead>
          <tr><th>区段编号</th><th>班次</th><th>起点</th><th>终点</th><th>缺点数</th><th>补回</th><th>仍缺</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in sheet.reconciliation['补采区段']" :key="item['区段编号']">
            <td>{{ item['区段编号'] }}</td>
            <td>{{ item['班次'] }}</td>
            <td>{{ item['起点'] }}</td>
            <td>{{ item['终点'] }}</td>
            <td>{{ item['缺失点数'] }}</td>
            <td>{{ item['补回点数'] }}</td>
            <td>{{ item['仍缺点数'] }}</td>
          </tr>
          <tr v-if="!sheet.reconciliation['补采区段'].length">
            <td colspan="7" class="empty-state">采样时刻齐全，没有缺点区段</td>
          </tr>
        </tbody>
      </table>

      <h4>连续高温复采区段</h4>
      <table class="data-table" data-testid="high-temp-ranges">
        <thead>
          <tr><th>区段编号</th><th>班次</th><th>起点</th><th>终点</th><th>连续点数</th><th>峰值</th><th>规则</th><th>派发</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in sheet.reconciliation['高温复采区段']" :key="item['区段编号']">
            <td>{{ item['区段编号'] }}</td>
            <td>{{ item['班次'] }}</td>
            <td>{{ item['起点'] }}</td>
            <td>{{ item['终点'] }}</td>
            <td>{{ item['连续点数'] }}</td>
            <td>{{ item['峰值'] }}</td>
            <td>{{ item['触发规则'] }}</td>
            <td>{{ item['派发'] }}</td>
          </tr>
          <tr v-if="!sheet.reconciliation['高温复采区段'].length">
            <td colspan="8" class="empty-state">没有连续超温区段</td>
          </tr>
        </tbody>
      </table>

      <h4>证据留档（原值不会被覆盖）</h4>
      <table class="data-table" data-testid="evidence-list">
        <thead>
          <tr><th>证据编号</th><th>采样时刻</th><th>数值</th><th>来源</th><th>归属班次</th><th>状态</th><th>计量签认</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in sheet.handover['证据']" :key="item['证据编号']">
            <td>{{ item['证据编号'] }}</td>
            <td>{{ item['采样时刻'] }}</td>
            <td>{{ item['数值'] }}</td>
            <td>{{ item['来源类型'] }}</td>
            <td>{{ item['归属班次'] }}</td>
            <td>{{ item['状态'] }}</td>
            <td>{{ item['计量签认']?.['签认班次'] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <form v-if="sheet.permissions['可提交证据']" class="analysis-form" @submit.prevent="submitEvidence">
        <label>
          <span>采样时刻</span>
          <input v-model="evidenceForm.moment" data-testid="evidence-moment" placeholder="2026-11-24T07:59:00" />
        </label>
        <label>
          <span>数值</span>
          <input v-model="evidenceForm.value" data-testid="evidence-value" type="number" step="0.1" />
        </label>
        <label>
          <span>来源类型</span>
          <select v-model="evidenceForm.source_type" data-testid="evidence-source">
            <option value="终端原件">终端原件</option>
            <option value="校准缓存">校准缓存</option>
            <option value="补采">补采</option>
          </select>
        </label>
        <button class="btn primary" data-testid="submit-evidence" type="submit">送到第二份值</button>
      </form>

      <form v-if="sheet.permissions['可签认缓存'] && sheet.reconciliation['待计量签认'].length" class="analysis-form" @submit.prevent="signOff">
        <label>
          <span>待签认证据</span>
          <select v-model="signForm.evidence_id" data-testid="signoff-evidence">
            <option v-for="item in sheet.reconciliation['待计量签认']" :key="item['证据编号'][0]" :value="item['证据编号'][0]">
              {{ item['证据编号'][0] }}（{{ item['采样时刻'] }} / {{ item['采用数值'] }}）
            </option>
          </select>
        </label>
        <label>
          <span>签认班次</span>
          <select v-model="signForm.shift" data-testid="signoff-shift">
            <option value="旧班">旧班</option>
            <option value="新班">新班</option>
          </select>
        </label>
        <button class="btn primary" data-testid="submit-signoff" type="submit">签认缓存值</button>
      </form>
    </template>

    <section v-if="summary" class="heartbeat-summary" data-testid="heartbeat-summary">
      <header class="panel-head">
        <div>
          <h3>采集心跳汇总 · {{ summary['汇总编号'] }}</h3>
          <p class="page-desc">
            终端 {{ summary['采集终端'] }} / 测点 {{ summary['测点编号'] }}；
            关联落盘点 旧班 {{ summary['关联落盘点']['旧班'] }}、新班 {{ summary['关联落盘点']['新班'] }}。
          </p>
        </div>
        <span class="role-hint">{{ summary['交接状态'] }}</span>
      </header>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">常规缺点</span>
          <strong class="stat-value" data-testid="summary-defects">{{ summary['常规缺点']['合计缺点'] }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">高温复采区段</span>
          <strong class="stat-value" data-testid="summary-hot">{{ summary['高温复采区段'].length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">补采仍缺点数</span>
          <strong class="stat-value" data-testid="summary-open">{{ summary['补采区段']['仍缺点数'] }}</strong>
        </article>
      </div>

      <h4>常规缺点（仅实际未齐的采样时刻）</h4>
      <table class="data-table">
        <thead><tr><th>班次</th><th>起点</th><th>终点</th><th>缺失点数</th></tr></thead>
        <tbody>
          <tr v-for="item in summary['常规缺点']['区段']" :key="item['区段编号']">
            <td>{{ item['班次'] }}</td>
            <td>{{ item['起点'] }}</td>
            <td>{{ item['终点'] }}</td>
            <td>{{ item['缺失点数'] }}</td>
          </tr>
        </tbody>
      </table>

      <h4>实际发出的高温复采区段（即使采样时刻齐全）</h4>
      <table class="data-table">
        <thead><tr><th>班次</th><th>起点</th><th>终点</th><th>连续点数</th><th>峰值</th><th>派发</th></tr></thead>
        <tbody>
          <tr v-for="item in summary['高温复采区段']" :key="item['区段编号']">
            <td>{{ item['班次'] }}</td>
            <td>{{ item['起点'] }}</td>
            <td>{{ item['终点'] }}</td>
            <td>{{ item['连续点数'] }}</td>
            <td>{{ item['峰值'] }}</td>
            <td>{{ item['派发'] }}</td>
          </tr>
          <tr v-if="!summary['高温复采区段'].length">
            <td colspan="6" class="empty-state">没有连续三次超过阈值的区段</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  loadHandoverSheet,
  loadHeartbeatSummary,
  signOffHandover,
  submitHandoverEvidence,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import type { HandoverSheet, HeartbeatSummary } from '@/api/backend-service'

const props = defineProps<{ entries: EntryRow[]; role: string }>()
const emit = defineEmits<{ (event: 'update:role', value: string): void }>()

const roles = [
  { value: '旧班负责人', label: '旧班负责人' },
  { value: '接班人', label: '接班人' },
  { value: '计量负责人', label: '计量负责人' },
  { value: '观察者', label: '观察者（只读）' },
]
const role = computed({
  get: () => props.role,
  set: (value: string) => emit('update:role', value),
})
const selectedId = ref<number | null>(null)
const sheet = ref<HandoverSheet | null>(null)
const summary = ref<HeartbeatSummary | null>(null)
const errorMessage = ref('')
const notice = ref('')
const boundary = computed(() => String(sheet.value?.handover['班界时刻'] ?? ''))

function shiftOf(moment: string): string {
  return boundary.value && moment < boundary.value ? '旧班' : '新班'
}
const evidenceForm = reactive({ moment: '', value: '', source_type: '校准缓存' })
const signForm = reactive({ evidence_id: '', shift: '旧班' })

async function reloadSheet() {
  errorMessage.value = ''
  notice.value = ''
  if (selectedId.value === null) {
    if (props.entries.length) selectedId.value = Number(props.entries[0].id)
    else return
  }
  try {
    sheet.value = await loadHandoverSheet(selectedId.value as number, role.value)
    summary.value = null
    const pending = sheet.value.reconciliation['待计量签认']
    if (pending.length) signForm.evidence_id = pending[0]['证据编号'][0]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交接表读取失败'
  }
}

async function loadSummary() {
  errorMessage.value = ''
  if (selectedId.value === null) return
  try {
    summary.value = await loadHeartbeatSummary(selectedId.value, role.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '心跳汇总读取失败'
  }
}

async function submitEvidence() {
  errorMessage.value = ''
  notice.value = ''
  if (selectedId.value === null) return
  const value = Number(evidenceForm.value)
  if (!evidenceForm.moment || Number.isNaN(value)) {
    errorMessage.value = '请填写采样时刻和数值'
    return
  }
  const result = await submitHandoverEvidence(selectedId.value, {
    moment: evidenceForm.moment,
    value,
    source_type: evidenceForm.source_type,
    role: role.value,
  })
  if (!result.ok) {
    errorMessage.value = result.message
    return
  }
  notice.value = result.message
  await reloadSheet()
  await loadSummary()
}

async function signOff() {
  errorMessage.value = ''
  notice.value = ''
  if (selectedId.value === null || !signForm.evidence_id) return
  const result = await signOffHandover(selectedId.value, {
    evidence_id: signForm.evidence_id,
    shift: signForm.shift,
    role: role.value,
  })
  if (!result.ok) {
    errorMessage.value = result.message
    return
  }
  notice.value = result.message
  await reloadSheet()
  await loadSummary()
}

watch(() => props.entries, (entries) => {
  if (!entries.length) return
  if (selectedId.value === null) selectedId.value = Number(entries[0].id)
  void reloadSheet()
}, { immediate: true })

onMounted(() => {
  if (selectedId.value === null && props.entries.length) void reloadSheet()
})
</script>

<style scoped>
.handover-panel { margin-top: 18px; border-top: 2px solid var(--border); padding-top: 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.handover-toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; margin: 10px 0 14px; }
.handover-toolbar label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.role-picker { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.role-hint { font-size: 12px; color: var(--muted); }
.notice-text { color: #067647; font-size: 13px; }
.muted { color: var(--muted); }
.heartbeat-summary { margin-top: 18px; padding-top: 14px; border-top: 1px dashed var(--border); }
h4 { margin: 16px 0 6px; font-size: 14px; }
</style>
