<template>
  <section class="page" data-module="dwd_layer">
    <header class="page-head">
      <div>
        <h2>明细层管理管理</h2>
        <p class="page-desc">维护明细层DWD表的输入、执行约束与结果记录，为明细层管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记明细表</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出明细层管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action.name"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action.name}`"
              @click="runAction(action, row)"
            >
              {{ action.name }}
            </button>
            <button class="link" data-testid="build-dwd" type="button" @click="buildDwd(row)">刷新明细</button>
            <button class="link" data-testid="parent-manifest" type="button" @click="openParentManifest(row)">父层清单</button>
            <span v-if="!availableActions(row).length" class="muted">已到终态</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无明细层管理数据，可先登记明细表</td>
        </tr>
      </tbody>
    </table>

    <div v-if="parentInfo" class="modal-backdrop" data-testid="parent-manifest-modal" @click.self="parentInfo = null">
      <div class="modal">
        <h3>父层贴源表清单结果</h3>
        <dl class="detail-grid">
          <dt>贴源表编号</dt><dd>{{ parentInfo['表编号'] }}</dd>
          <dt>贴源表名称</dt><dd>{{ parentInfo['表名称'] }}</dd>
          <dt>来源系统</dt><dd>{{ parentInfo['来源系统'] }}</dd>
          <dt>在线状态</dt>
          <dd>
            <span class="status-tag" :class="parentInfo['在线状态'] === '在线' ? 'tag-online' : 'tag-offline'">
              {{ parentInfo['在线状态'] }}
            </span>
            <span v-if="parentInfo['待切换']" class="status-tag tag-wait">待切换</span>
          </dd>
          <dt>清单阶段</dt><dd>{{ parentInfo['阶段'] }}（归档→恢复→切换 单向推进）</dd>
          <dt>清单顺序号</dt><dd>{{ parentInfo['顺序号'] }}</dd>
        </dl>
        <h4>父层引用结果</h4>
        <table class="data-table small">
          <thead><tr><th>明细表编号</th><th>明细表名称</th><th>引用状态</th></tr></thead>
          <tbody>
            <tr v-for="(ref, idx) in parentInfo['父层引用']" :key="idx">
              <td>{{ ref['明细表编号'] }}</td><td>{{ ref['明细表名称'] }}</td><td>{{ ref['引用状态'] }}</td>
            </tr>
            <tr v-if="!parentInfo['父层引用'].length"><td colspan="3" class="empty-state">暂无父层引用</td></tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn" type="button" @click="parentInfo = null">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记明细表</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条明细层管理记录；已下线贴源表不能作为新明细父层，刷新读到下线父层会被拒绝</span>
      <span v-if="errorMessage" class="error-text" data-testid="action-error">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type ParentInfo = {
  顺序号: number
  表编号: string
  表名称: string
  来源系统: string
  在线状态: string
  阶段: string
  待切换: boolean
  父层引用: { 明细表编号: string; 明细表名称: string; 引用状态: string }[]
}

const ENDPOINT = '/api/dwd_layer'
const columns = ["表编号", "父层来源", "表名称", "粒度说明", "分区字段", "关联键列", "刷新水位", "创建批次"]
// 明细状态同样只能按顺序单向推进
const NEXT_ACTIONS: Record<string, { name: string; target: string }[]> = {
  "草稿": [{ name: "提交校验", target: "待校验" }],
  "待校验": [{ name: "确认上线", target: "已上线" }],
  "已上线": [{ name: "标记下线", target: "已下线" }],
  "已下线": [],
}
const statuses = ["草稿", "待校验", "已上线", "已下线"]
const metricNames = ["明细表总数", "待校验表", "已上线表"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const parentInfo = ref<ParentInfo | null>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
])

function availableActions(row: Row) {
  return NEXT_ACTIONS[String(row.status ?? '')] ?? []
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'dwd_layer.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: { name: string; target: string }, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action: action.name }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '明细层管理动作未生效')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细层管理操作失败'
  }
}

async function buildDwd(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/build`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '明细刷新失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细刷新失败'
  }
}

async function openParentManifest(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/parent_manifest`)
    if (!response.ok) {
      const payload = await response.json().catch(() => ({ detail: '父层清单读取失败' }))
      throw new Error(payload.detail || '父层清单读取失败')
    }
    parentInfo.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '父层清单读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('明细表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '明细层管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.muted { color: var(--muted); font-size: 12px; }
.status-tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.tag-online { background: #dcfce7; color: #15803d; }
.tag-offline { background: #fee2e2; color: #b91c1c; }
.tag-wait { background: #fef3c7; color: #b45309; }
.detail-grid { display: grid; grid-template-columns: 110px 1fr; gap: 6px 12px; font-size: 13px; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.modal h4 { margin: 16px 0 6px; font-size: 13px; }
.small th, .small td { font-size: 12px; padding: 6px 8px; }
</style>
