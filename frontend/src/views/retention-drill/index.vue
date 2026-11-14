<template>
  <section class="page" data-module="retention-drill">
    <header class="page-head">
      <div>
        <h2>保留例外演练</h2>
        <p class="page-desc">
          规则生效前预演例外清单冲突：列出将被延期、转为归档或进入回收站的表与冲突来源；
          状态机严格按 草稿→生效→到期→废止 流转，演练结果写入审计并同步生命周期治理清单。
        </p>
      </div>
    </header>

    <div class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="btn"
        :class="{ primary: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- ============================================================ 例外规则 -->
    <div v-if="activeTab === 'exceptions'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">例外规则总数</span>
          <strong class="stat-value">{{ exceptionStats.total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">生效中</span>
          <strong class="stat-value">{{ exceptionStats.active }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">草稿/到期/废止</span>
          <strong class="stat-value">{{ exceptionStats.other }}</strong>
        </article>
      </div>

      <form class="filter-bar" @submit.prevent="loadExceptions">
        <label class="filter-item">
          <span>例外编号</span>
          <input v-model="exceptionFilters.keyword" placeholder="按例外编号检索" />
        </label>
        <label class="filter-item">
          <span>状态</span>
          <select v-model="exceptionFilters.status">
            <option value="">全部</option>
            <option v-for="s in statusOrder" :key="s" :value="s">{{ s }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>版本</span>
          <input v-model="exceptionFilters.version" placeholder="如 v1" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetExceptionFilters">重置</button>
        <button class="btn primary" type="button" @click="openCreate">登记例外规则</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in exceptionColumns" :key="column">{{ column }}</th>
            <th>当前状态</th>
            <th>状态机动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in exceptions" :key="String(row.id)">
            <td v-for="column in exceptionColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>
              <span class="status-badge" :class="statusClass(String(row.status))">{{ row.status }}</span>
            </td>
            <td class="row-actions">
              <button
                v-for="action in nextActions(String(row.status))"
                :key="action"
                class="link"
                type="button"
                @click="transition(row, action)"
              >
                {{ action }}
              </button>
              <span v-if="!nextActions(String(row.status)).length" class="muted-text">已到终态</span>
            </td>
          </tr>
          <tr v-if="!exceptions.length">
            <td :colspan="exceptionColumns.length + 2" class="empty-state">暂无例外规则</td>
          </tr>
        </tbody>
      </table>
      <p class="state-hint">状态机只允许相邻前进：草稿 → 生效 → 到期 → 废止，回退或跳过中间状态会被后端拒绝。</p>
    </div>

    <!-- ============================================================ 演练记录 -->
    <div v-if="activeTab === 'drills'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">演练单总数</span>
          <strong class="stat-value">{{ drillStats.total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">覆盖表数</span>
          <strong class="stat-value">{{ drillStats.tables }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">阻止永久清理</span>
          <strong class="stat-value">{{ drillStats.blocked }}</strong>
        </article>
      </div>

      <form class="filter-bar" @submit.prevent="loadDrills">
        <label class="filter-item">
          <span>演练版本</span>
          <input v-model="drillVersion" placeholder="输入版本，如 v1" />
        </label>
        <button class="btn" type="submit">查询演练单</button>
        <button class="btn primary" type="button" :disabled="drilling" @click="runDrill">
          {{ drilling ? '演练执行中…' : '执行保留例外演练' }}
        </button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>演练单号</th>
            <th>版本</th>
            <th>演练时间</th>
            <th>生效例外</th>
            <th>覆盖表</th>
            <th>延期/归档/回收站</th>
            <th>双重命中</th>
            <th>阻止永久清理</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in drills" :key="String(row.id)">
            <td>{{ row['演练单号'] }}</td>
            <td>{{ row['版本'] }}</td>
            <td>{{ row['演练时间'] }}</td>
            <td>{{ row['生效例外数'] }}</td>
            <td>{{ row['覆盖表数'] }}</td>
            <td>{{ row['延期保留数'] }} / {{ row['转为归档数'] }} / {{ row['回收站保护数'] }}</td>
            <td>{{ row['双重命中数'] }}</td>
            <td>
              <span :class="row['阻止永久清理数'] ? 'warn-text' : 'muted-text'">
                {{ row['阻止永久清理数'] }}
              </span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="openDetail(row)">查看明细与冲突</button>
            </td>
          </tr>
          <tr v-if="!drills.length">
            <td colspan="9" class="empty-state">暂无演练单，输入版本后执行演练</td>
          </tr>
        </tbody>
      </table>
      <p class="state-hint">
        同一版本重复演练只保留一份有效演练单（幂等）；同一数据集范围并发演练时，后到请求会被 409 拒绝。
      </p>
    </div>

    <!-- ============================================================ 治理清单 -->
    <div v-if="activeTab === 'governance'">
      <form class="filter-bar" @submit.prevent="loadGovernance">
        <label class="filter-item">
          <span>表名 / 演练单号</span>
          <input v-model="governanceKeyword" placeholder="按表名或演练单号检索" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="governanceKeyword = ''; loadGovernance()">
          重置
        </button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in governanceColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in governanceRows" :key="String(row.id)">
            <td v-for="column in governanceColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!governanceRows.length">
            <td :colspan="governanceColumns.length" class="empty-state">
              治理清单为空，执行演练后自动同步
            </td>
          </tr>
        </tbody>
      </table>
      <p class="state-hint">每行带演练单号，可回溯到演练明细与审计依据；同版本重复演练按表覆盖，不产生重复行。</p>
    </div>

    <!-- ============================================================ 演练明细弹层 -->
    <div v-if="detailDrill" class="modal-mask" @click.self="detailDrill = null">
      <div class="modal-box">
        <div class="modal-head">
          <strong>演练明细 · {{ detailDrill['演练单号'] }}（版本 {{ detailDrill['版本'] }}）</strong>
          <button class="link" type="button" @click="detailDrill = null">关闭</button>
        </div>
        <p class="state-hint">
          基准日期 {{ detailDrill['基准日期'] }} ｜ 覆盖 {{ detailDrill['覆盖表数'] }} 张表 ｜
          双重命中 {{ detailDrill['双重命中数'] }} 处 ｜ 阻止永久清理 {{ detailDrill['阻止永久清理数'] }} 处
        </p>
        <div v-for="item in detailDrill['演练明细']" :key="item['表名称']" class="drill-card">
          <div class="drill-card-head">
            <strong>{{ item['表名称'] }}</strong>
            <span class="status-badge" :class="outcomeClass(item['处置结果'])">{{ item['处置结果'] }}</span>
            <span v-if="item['禁止永久清理']" class="warn-tag">禁止永久清理</span>
          </div>
          <dl class="drill-meta">
            <div><dt>命中例外</dt><dd>{{ item['命中例外'].join('、') }}</dd></div>
            <div><dt>裁决胜出</dt><dd>{{ item['裁决胜出例外'] }}（{{ item['裁决合规依据'] }}，权重{{ item['合规权重'] }}）</dd></div>
            <div><dt>保留天数</dt><dd>{{ item['裁决保留天数'] }} 天</dd></div>
            <div><dt>保护截止日</dt><dd>{{ item['保护截止日'] }}</dd></div>
          </dl>
          <p class="drill-reason">{{ item['裁决依据'] }}</p>
          <table class="data-table conflict-table">
            <thead>
              <tr><th>冲突来源类型</th><th>来源编号</th><th>冲突说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="(conflict, idx) in item['冲突来源']" :key="idx">
                <td>{{ conflict['来源类型'] }}</td>
                <td>{{ conflict['来源编号'] }}</td>
                <td>{{ conflict['冲突说明'] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ============================================================ 登记弹层 -->
    <div v-if="creating" class="modal-mask" @click.self="creating = false">
      <div class="modal-box narrow">
        <div class="modal-head">
          <strong>登记保留例外规则（初始状态：草稿）</strong>
          <button class="link" type="button" @click="creating = false">关闭</button>
        </div>
        <form class="create-form" @submit.prevent="submitCreate">
          <label v-for="field in createFields" :key="field" class="filter-item">
            <span>{{ field }}<em v-if="requiredFields.includes(field)" class="warn-text">*</em></span>
            <input v-model="createForm[field]" :placeholder="`请输入${field}`" />
          </label>
          <div class="modal-actions">
            <button class="btn primary" type="submit">提交登记</button>
            <button class="btn ghost" type="button" @click="creating = false">取消</button>
          </div>
        </form>
      </div>
    </div>

    <footer class="page-foot">
      <span>{{ footerText }}</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/retention-drill'

type Row = Record<string, string | number | boolean | null>

interface ConflictSource {
  来源类型: string
  来源编号: string | null
  来源记录ID: number | null
  冲突说明: string
  禁止永久清理?: boolean
}

interface DrillDetailItem {
  表名称: string
  处置结果: string
  处置原因: string
  命中例外: string[]
  裁决胜出例外: string
  裁决合规依据: string
  合规权重: number
  裁决保留天数: number
  保护截止日: string
  禁止永久清理: boolean
  冲突来源: ConflictSource[]
  裁决依据: string
}

type DrillRow = Row & { '演练明细'?: DrillDetailItem[] }

const tabs = [
  { key: 'exceptions', label: '例外规则' },
  { key: 'drills', label: '演练记录' },
  { key: 'governance', label: '生命周期治理清单' },
] as const
type TabKey = (typeof tabs)[number]['key']
const activeTab = ref<TabKey>('exceptions')

const statusOrder = ['草稿', '生效', '到期', '废止']
const actionByStatus: Record<string, string[]> = {
  草稿: ['提交生效'],
  生效: ['标记到期'],
  到期: ['执行废止'],
  废止: [],
}
const exceptionColumns = ['例外编号', '数据集范围', '保留期限', '过期动作', '合规依据', '版本']
const governanceColumns = [
  '治理编号', '表名称', '版本', '处置结果', '保护来源', '合规依据',
  '保护截止日', '禁止永久清理', '冲突来源', '演练单号', '同步时间',
]
const requiredFields = ['例外编号', '数据集范围', '保留期限', '过期动作', '合规依据']
const createFields = [...requiredFields, '版本']

const exceptions = ref<Row[]>([])
const drills = ref<DrillRow[]>([])
const governanceRows = ref<Row[]>([])
const message = ref('')
const messageOk = ref(true)
const drilling = ref(false)
const detailDrill = ref<DrillRow | null>(null)
const creating = ref(false)
const createForm = reactive<Record<string, string>>({})
const drillVersion = ref('v1')
const governanceKeyword = ref('')
const exceptionFilters = reactive({ keyword: '', status: '', version: '' })

const exceptionStats = computed(() => ({
  total: exceptions.value.length,
  active: exceptions.value.filter((row) => row.status === '生效').length,
  other: exceptions.value.filter((row) => row.status !== '生效').length,
}))
const drillStats = computed(() => ({
  total: drills.value.length,
  tables: drills.value.reduce((sum, row) => sum + Number(row['覆盖表数'] ?? 0), 0),
  blocked: drills.value.reduce((sum, row) => sum + Number(row['阻止永久清理数'] ?? 0), 0),
}))
const footerText = computed(() => {
  if (activeTab.value === 'exceptions') return `共 ${exceptions.value.length} 条例外规则`
  if (activeTab.value === 'drills') return `共 ${drills.value.length} 张演练单`
  return `治理清单 ${governanceRows.value.length} 行`
})

function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function nextActions(status: string): string[] {
  return actionByStatus[status] ?? []
}

function statusClass(status: string): string {
  if (status === '生效') return 'badge-green'
  if (status === '废止') return 'badge-gray'
  if (status === '到期') return 'badge-amber'
  return 'badge-blue'
}

function outcomeClass(outcome: string): string {
  if (outcome === '回收站保护') return 'badge-amber'
  if (outcome === '转为归档') return 'badge-blue'
  return 'badge-green'
}

async function loadExceptions() {
  const query = new URLSearchParams()
  if (exceptionFilters.keyword) query.set('keyword', exceptionFilters.keyword)
  if (exceptionFilters.status) query.set('status', exceptionFilters.status)
  if (exceptionFilters.version) query.set('version', exceptionFilters.version)
  const response = await request(`${ENDPOINT}/exceptions?${query.toString()}`)
  if (!response.ok) {
    notify('例外规则读取失败', false)
    return
  }
  const payload = await response.json()
  exceptions.value = payload.items ?? []
}

function resetExceptionFilters() {
  exceptionFilters.keyword = ''
  exceptionFilters.status = ''
  exceptionFilters.version = ''
  void loadExceptions()
}

async function transition(row: Row, action: string) {
  const response = await request(`${ENDPOINT}/exceptions/${row.id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ values: { action } }),
  })
  const payload = await response.json()
  if (!response.ok || !payload.ok) {
    notify(payload.message ?? '状态流转被拒绝', false)
    return
  }
  notify(payload.message)
  await loadExceptions()
}

function openCreate() {
  createFields.forEach((field) => {
    createForm[field] = field === '版本' ? 'v1' : ''
  })
  creating.value = true
}

async function submitCreate() {
  const response = await request(`${ENDPOINT}/exceptions`, {
    method: 'POST',
    body: JSON.stringify({ values: { ...createForm } }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    notify(payload.message ?? '登记失败', false)
    return
  }
  creating.value = false
  notify('例外规则已登记为草稿')
  await loadExceptions()
}

async function loadDrills() {
  const query = new URLSearchParams()
  if (drillVersion.value) query.set('version', drillVersion.value)
  const response = await request(`${ENDPOINT}/drills?${query.toString()}`)
  if (!response.ok) {
    notify('演练单读取失败', false)
    return
  }
  const payload = await response.json()
  drills.value = payload.items ?? []
}

async function runDrill() {
  if (!drillVersion.value.trim()) {
    notify('请先输入演练版本', false)
    return
  }
  drilling.value = true
  try {
    const response = await request(`${ENDPOINT}/drills`, {
      method: 'POST',
      body: JSON.stringify({ values: { 版本: drillVersion.value.trim() } }),
    })
    const payload = await response.json()
    if (!response.ok) {
      // 409 并发争用 / 400 无生效例外等，detail 里是后端的可读原因。
      notify(payload.detail ?? '演练未执行', false)
      return
    }
    notify(payload.message)
    await loadDrills()
  } finally {
    drilling.value = false
  }
}

function openDetail(row: DrillRow) {
  detailDrill.value = row
}

async function loadGovernance() {
  const query = new URLSearchParams()
  if (governanceKeyword.value) query.set('keyword', governanceKeyword.value)
  const response = await request(`${ENDPOINT}/governance?${query.toString()}`)
  if (!response.ok) {
    notify('治理清单读取失败', false)
    return
  }
  const payload = await response.json()
  governanceRows.value = payload.items ?? []
}

function switchTab(key: TabKey) {
  activeTab.value = key
  message.value = ''
}

onMounted(() => {
  void loadExceptions()
  void loadDrills()
  void loadGovernance()
})
</script>

<style scoped>
.tab-bar { display: flex; gap: 8px; margin-bottom: 12px; }
.status-badge {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 12px; color: #fff; white-space: nowrap;
}
.badge-green { background: #178f4d; }
.badge-blue { background: #1f6feb; }
.badge-amber { background: #b7791f; }
.badge-gray { background: #64748b; }
.muted-text { color: var(--muted); font-size: 12px; }
.warn-text { color: #b42318; font-weight: 600; }
.ok-text { color: #178f4d; }
.warn-tag {
  margin-left: 8px; padding: 2px 8px; border-radius: 4px;
  background: #fef3f2; color: #b42318; border: 1px solid #f0b7b0; font-size: 12px;
}
.state-hint { color: var(--muted); font-size: 12px; margin-top: 8px; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 20;
}
.modal-box {
  background: #fff; border-radius: 10px; width: min(960px, 92vw);
  max-height: 86vh; overflow: auto; padding: 18px 20px;
}
.modal-box.narrow { width: min(520px, 92vw); }
.modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.drill-card {
  border: 1px solid var(--border); border-radius: 8px;
  padding: 12px; margin-bottom: 12px; background: #fbfdff;
}
.drill-card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.drill-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 16px; margin: 0 0 8px; }
.drill-meta dt { color: var(--muted); font-size: 12px; display: inline; margin-right: 6px; }
.drill-meta dd { display: inline; margin: 0; font-size: 13px; }
.drill-reason { font-size: 13px; background: #f1f5ff; border-radius: 6px; padding: 8px; margin: 0 0 8px; }
.conflict-table th, .conflict-table td { font-size: 12px; }
.create-form { display: flex; flex-direction: column; gap: 10px; }
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; }
select {
  border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px;
  font-size: 13px; background: #fff; min-width: 120px;
}
</style>
