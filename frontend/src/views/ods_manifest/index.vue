<template>
  <section class="page" data-module="ods_manifest">
    <header class="page-head">
      <div>
        <h2>ODS表版本清单</h2>
        <p class="page-desc">
          数据来自已上线贴源表，清单只保留在线与下线顺序、阶段及DWD父层引用结果；
          阶段按归档→恢复→切换单向推进。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="sync-manifest" type="button" @click="syncManifest">刷新清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">清单条目</span>
        <strong class="stat-value">{{ rows.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">在线</span>
        <strong class="stat-value">{{ onlineCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已下线</span>
        <strong class="stat-value">{{ offlineCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待切换/中断</span>
        <strong class="stat-value">{{ pendingCount }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>在线状态</span>
        <select v-model="statusFilter" data-testid="filter-status" @change="reload">
          <option value="">全部</option>
          <option value="在线">在线</option>
          <option value="已下线">已下线</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
    </form>

    <table class="data-table" data-testid="manifest-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>父层引用结果</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row['表编号']" :data-testid="`row-${row['表编号']}`" :data-status="row['在线状态']">
          <td>{{ row['顺序号'] }}</td>
          <td>{{ row['表编号'] }}</td>
          <td>{{ row['表名称'] }}</td>
          <td>{{ row['来源系统'] }}</td>
          <td>
            <span class="status-tag" :class="row['在线状态'] === '在线' ? 'tag-online' : 'tag-offline'">
              {{ row['在线状态'] }}
            </span>
            <span v-if="row['待切换']" class="status-tag tag-wait">待切换</span>
            <span v-if="row['中断中']" class="status-tag tag-broken">中断中</span>
          </td>
          <td>
            <span class="stage-tag" v-for="stage in stageOrder" :key="stage"
              :class="stageClass(row['阶段'], stage)">{{ stage }}</span>
          </td>
          <td>
            <span v-if="!row['父层引用'].length" class="muted">无DWD引用</span>
            <ul v-else class="ref-list">
              <li v-for="ref in row['父层引用']" :key="ref['明细表编号']">
                {{ ref['明细表编号'] }}
                <span class="status-tag" :class="ref['引用状态'] === '等待父层清单' ? 'tag-wait' : 'tag-offline'">
                  {{ ref['引用状态'] }}
                </span>
              </li>
            </ul>
          </td>
          <td class="row-actions">
            <button class="link" data-testid="view-detail" type="button" @click="openDetail(row['表编号'])">清单详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state" data-testid="empty-state">
            {{ emptyMessage || 'ODS表版本清单暂无数据：当前没有已上线贴源表' }}
          </td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ rows.length }} 条清单记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="selected" class="modal-backdrop" data-testid="detail-modal" @click.self="selected = null">
      <div class="modal">
        <h3>清单详情：{{ selected['表编号'] }}</h3>
        <dl class="detail-grid">
          <dt>顺序号</dt><dd>{{ selected['顺序号'] }}</dd>
          <dt>表名称</dt><dd>{{ selected['表名称'] }}</dd>
          <dt>来源系统</dt><dd>{{ selected['来源系统'] }}</dd>
          <dt>在线状态</dt><dd>{{ selected['在线状态'] }}</dd>
          <dt>当前阶段</dt><dd>{{ selected['阶段'] }}</dd>
          <dt>待切换</dt><dd>{{ selected['待切换'] ? '是（等待DWD刷新完成后下线）' : '否' }}</dd>
        </dl>

        <h4>阶段历史（单向推进，归档→恢复→切换）</h4>
        <table class="data-table small">
          <thead><tr><th>阶段</th><th>时间</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="(item, idx) in selected['阶段历史']" :key="idx">
              <td>{{ item['阶段'] }}</td><td>{{ item['时间'] }}</td><td>{{ item['说明'] }}</td>
            </tr>
          </tbody>
        </table>

        <h4>DWD父层引用</h4>
        <table class="data-table small">
          <thead><tr><th>明细表编号</th><th>明细表名称</th><th>引用状态</th><th>明细表状态</th><th>记录时间</th></tr></thead>
          <tbody>
            <tr v-for="(ref, idx) in selected['父层引用']" :key="idx">
              <td>{{ ref['明细表编号'] }}</td><td>{{ ref['明细表名称'] }}</td>
              <td>{{ ref['引用状态'] }}</td><td>{{ ref['明细表状态'] }}</td><td>{{ ref['记录时间'] }}</td>
            </tr>
            <tr v-if="!selected['父层引用'].length"><td colspan="5" class="empty-state">暂无父层引用</td></tr>
          </tbody>
        </table>

        <h4>下线/刷新冲突记录（两种结果均保留）</h4>
        <table class="data-table small">
          <thead><tr><th>时间</th><th>采用策略</th><th>下线优先结果</th><th>刷新优先结果</th></tr></thead>
          <tbody>
            <tr v-for="(item, idx) in selected['冲突记录']" :key="idx">
              <td>{{ item['时间'] }}</td><td>{{ item['采用策略'] }}</td>
              <td>{{ item['结果一_下线优先'] }}</td><td>{{ item['结果二_刷新优先'] }}</td>
            </tr>
            <tr v-if="!selected['冲突记录'].length"><td colspan="4" class="empty-state">暂无冲突记录</td></tr>
          </tbody>
        </table>

        <div class="page-actions">
          <button v-if="selected['中断中']" class="btn primary" data-testid="retry-interrupt" type="button" @click="retry(selected['表编号'])">
            先完成当前刷新再提交下线（重试）
          </button>
          <button class="btn" type="button" @click="selected = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/ods_manifest'
const columns = ['顺序号', '表编号', '表名称', '来源系统', '在线状态', '阶段']
const stageOrder = ['归档', '恢复', '切换']

type ManifestRow = {
  顺序号: number
  表编号: string
  表名称: string
  来源系统: string
  在线状态: string
  阶段: string
  待切换: boolean
  中断中: boolean
  父层引用: { 明细表编号: string; 明细表名称: string; 引用状态: string }[]
}

type ManifestDetail = ManifestRow & {
  阶段历史: { 阶段: string; 时间: string; 说明: string }[]
  父层引用: { 明细表编号: string; 明细表名称: string; 引用状态: string; 明细表状态: string; 记录时间: string }[]
  冲突记录: { 时间: string; 采用策略: string; 结果一_下线优先: string; 结果二_刷新优先: string }[]
}

const rows = ref<ManifestRow[]>([])
const selected = ref<ManifestDetail | null>(null)
const statusFilter = ref('')
const errorMessage = ref('')
const emptyMessage = ref('')

const onlineCount = computed(() => rows.value.filter((r) => r.在线状态 === '在线').length)
const offlineCount = computed(() => rows.value.filter((r) => r.在线状态 === '已下线').length)
const pendingCount = computed(() => rows.value.filter((r) => r.待切换 || r.中断中).length)

function stageClass(current: string, stage: string): string {
  const currentIdx = stageOrder.indexOf(current)
  const stageIdx = stageOrder.indexOf(stage)
  if (stageIdx < currentIdx) return 'stage-done'
  if (stageIdx === currentIdx) return 'stage-current'
  return 'stage-todo'
}

async function reload() {
  errorMessage.value = ''
  emptyMessage.value = ''
  try {
    const query = statusFilter.value ? `?status=${encodeURIComponent(statusFilter.value)}` : ''
    const response = await request(`${ENDPOINT}${query}`)
    if (!response.ok) throw new Error('ODS表版本清单读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    if (!rows.value.length && !statusFilter.value) {
      const hintResp = await request(`${ENDPOINT}/empty-hint`)
      if (hintResp.ok) {
        const hint = await hintResp.json()
        emptyMessage.value = hint.message || ''
      }
    } else if (!rows.value.length) {
      emptyMessage.value = `当前筛选条件下没有「${statusFilter.value}」的清单记录`
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'ODS表版本清单读取失败'
  }
}

async function syncManifest() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/sync`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '清单刷新失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单刷新失败'
  }
}

async function openDetail(odsCode: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${encodeURIComponent(odsCode)}`)
    if (!response.ok) throw new Error('清单详情读取失败')
    selected.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单详情读取失败'
  }
}

async function retry(odsCode: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${encodeURIComponent(odsCode)}/retry`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '重试失败')
    selected.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.status-tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; margin-right: 4px; }
.tag-online { background: #dcfce7; color: #15803d; }
.tag-offline { background: #fee2e2; color: #b91c1c; }
.tag-wait { background: #fef3c7; color: #b45309; }
.tag-broken { background: #e0e7ff; color: #4338ca; }
.stage-tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; margin-right: 4px; border: 1px solid var(--border); color: var(--muted); }
.stage-done { background: #e0f2fe; color: #0369a1; border-color: #bae6fd; }
.stage-current { background: var(--brand); color: #fff; border-color: var(--brand); }
.stage-todo { background: #f8fafc; }
.ref-list { margin: 0; padding-left: 14px; font-size: 12px; }
.ref-list li { margin-bottom: 2px; }
.muted { color: var(--muted); font-size: 12px; }
.detail-grid { display: grid; grid-template-columns: 90px 1fr; gap: 6px 12px; font-size: 13px; }
.detail-grid dt { color: var(--muted); }
.detail-grid dd { margin: 0; }
.modal h4 { margin: 16px 0 6px; font-size: 13px; }
.small th, .small td { font-size: 12px; padding: 6px 8px; }
select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
</style>
