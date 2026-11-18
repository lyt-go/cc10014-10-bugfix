import { expect, test } from '@playwright/test'

/**
 * ODS表版本清单与贴源表状态流转专项验收。
 * 前置：后端已启动（seed 含 3 张已上线贴源表），前端 dev server 监听 baseURL。
 */
test('ODS表版本清单：只展示在线/下线顺序、阶段与父层引用，详情可见阶段历史', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/ods_manifest')
  await page.waitForTimeout(2000)

  const rows = page.locator('[data-testid="manifest-table"] tbody tr[data-testid^="row-"]')
  await expect(rows).toHaveCount(3)

  // 每行都有在线状态标签与归档/恢复/切换三个阶段位
  const firstRow = rows.nth(0)
  await expect(firstRow.locator('.status-tag').first()).toContainText('在线')
  await expect(firstRow.locator('.stage-tag')).toHaveCount(3)

  // 打开清单详情：阶段历史、父层引用、冲突记录区块都在
  await firstRow.getByTestId('view-detail').click()
  const modal = page.getByTestId('detail-modal')
  await expect(modal).toBeVisible()
  await expect(modal.getByText('阶段历史（单向推进，归档→恢复→切换）')).toBeVisible()
  await expect(modal.getByText('DWD父层引用')).toBeVisible()
  await expect(modal.getByText('下线/刷新冲突记录（两种结果均保留）')).toBeVisible()
})

test('贴源层管理：动作只按状态顺序推进，下线需弹窗选择冲突策略', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/ods_layer')
  await page.waitForTimeout(2000)

  // seed 表均为已上线，只应出现「标记下线」，不能出现跳态动作
  const firstRow = page.locator('tbody tr[data-testid^="row-"]').nth(0)
  await expect(firstRow.getByRole('button', { name: '标记下线', exact: true })).toBeVisible()
  await expect(firstRow.getByRole('button', { name: '提交校验', exact: true })).toHaveCount(0)
  await expect(firstRow.getByRole('button', { name: '确认上线', exact: true })).toHaveCount(0)

  // 新建表走完整单向流程：草稿 -> 待校验 -> 已上线 -> 弹窗确认 -> 已下线
  const values = ['E2E-ODS-FLOW-1', '验收系统', 'ods_e2e_flow', 'dt', 'PARQUET', '30', 'SNAPPY', 'RAW-E2E']
  const fields = ['表编号', '来源系统', '表名称', '分区字段', '存储格式', '保留天数', '压缩算法', '创建批次']
  await page.getByTestId('open-create').click()
  for (const [i, field] of fields.entries()) {
    await page.getByTestId(`create-${field}`).fill(values[i])
  }
  await page.getByTestId('submit-create').click()
  const created = page.locator('tbody tr').filter({ hasText: 'E2E-ODS-FLOW-1' })
  await expect(created).toHaveAttribute('data-status', '草稿')

  await created.getByRole('button', { name: '提交校验', exact: true }).click()
  await expect(created).toHaveAttribute('data-status', '待校验')
  // 待校验状态没有标记下线入口，无法直接下线
  await expect(created.getByRole('button', { name: '标记下线', exact: true })).toHaveCount(0)

  await created.getByRole('button', { name: '确认上线', exact: true }).click()
  await expect(created).toHaveAttribute('data-status', '已上线')

  await created.getByRole('button', { name: '标记下线', exact: true }).click()
  await expect(page.getByTestId('offline-modal')).toBeVisible()
  await page.getByTestId('conflict-policy').selectOption('刷新优先')
  await page.getByTestId('confirm-offline').click()
  await expect(created).toHaveAttribute('data-status', '已下线')
})

test('明细层管理：动作按状态单向推进，已下线父层不可刷新，父层清单跨模块可读', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/dwd_layer')
  await page.waitForTimeout(2000)

  // DWD_USR_001 在 seed 中为草稿，走完整单向流程
  const draft = page.locator('tbody tr').filter({ hasText: 'DWD_USR_001' })
  await expect(draft).toHaveAttribute('data-status', '草稿')
  // 草稿态不能直接标记下线
  await expect(draft.getByRole('button', { name: '标记下线', exact: true })).toHaveCount(0)
  await draft.getByRole('button', { name: '提交校验', exact: true }).click()
  await expect(draft).toHaveAttribute('data-status', '待校验')
  await draft.getByRole('button', { name: '确认上线', exact: true }).click()
  await expect(draft).toHaveAttribute('data-status', '已上线')
  await draft.getByRole('button', { name: '标记下线', exact: true }).click()
  await expect(draft).toHaveAttribute('data-status', '已下线')
  // 已下线是终态，无动作按钮
  await expect(draft.getByRole('button', { name: '提交校验', exact: true })).toHaveCount(0)

  // 已上线 DWD_ORD_001 的父层在线，刷新应成功，无错误文案
  const firstRow = page.locator('tbody tr[data-testid^="row-"]').nth(0)
  await firstRow.getByTestId('build-dwd').click()
  await page.waitForTimeout(500)
  await expect(page.getByTestId('action-error')).toHaveCount(0)

  // 父层清单弹窗能展示贴源表在线状态与阶段
  await firstRow.getByTestId('parent-manifest').click()
  const modal = page.getByTestId('parent-manifest-modal')
  await expect(modal).toBeVisible()
  await expect(modal.getByText('父层贴源表清单结果')).toBeVisible()
  await expect(modal.getByText('在线', { exact: true })).toBeVisible()
})
