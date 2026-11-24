import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
  role?: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify(role ? { action, role } : { action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 班次交接：交接表、双份证据、心跳汇总 ----

export type HandoverEvidence = {
  '证据编号': string
  '采样时刻': string
  数值: number
  '来源类型': string
  '归属班次': string
  状态: string
  '计量签认': Record<string, string> | null
}

export type HandoverReconciliation = {
  '采用观测': Array<Record<string, unknown>>
  '缺点时刻': string[]
  '缺点区段': Array<{ 区段编号: string; 班次: string; 起点: string; 终点: string; 缺失点数: number }>
  '缺点合计': number
  '旧班缺点': number
  '新班缺点': number
  '旧班实收': number
  '新班实收': number
  '采用温度': string | number
  '交叠段采用': Array<Record<string, unknown>>
  '高温复采区段': Array<{
    区段编号: string; 班次: string; 起点: string; 终点: string
    连续点数: number; 峰值: number; 阈值: number; 触发规则: string; 派发: string
  }>
  '同一时刻多份证据': Array<{
    采样时刻: string; 采用证据: string; 采用数值: number; 采用来源: string; 采用依据: string
    未采用证据: string[]; 未采用数值: number[]; 未采用来源: string[]; 待计量签认: string[]
  }>
  '待计量签认': Array<{ 采样时刻: string; 采用数值: number; 证据编号: string[]; 归属班次: string }>
  '补采区段': Array<{
    区段编号: string; 班次: string; 起点: string; 终点: string; 缺失点数: number
    补回点数: number; 仍缺点数: number; 是否仍缺: boolean; 仍缺采样时刻: string[]
  }>
  '仍缺点数': number
}

export type HandoverSheet = {
  handover: EntryRow & { 证据: HandoverEvidence[]; 确认记录: Record<string, Record<string, unknown>>; 审计日志: Array<Record<string, unknown>> }
  role: string
  role_label: string
  permissions: Record<string, boolean | string>
  reconciliation: HandoverReconciliation
  audit_log: Array<Record<string, unknown>>
}

export type HeartbeatSummary = {
  '汇总编号': string
  '交接编号': string
  '采集终端': string
  '测点编号': string
  '关联落盘点': { 旧班: number; 新班: number; 合计: number; 采样时刻: string[] }
  '常规缺点': { 合计缺点: number; 旧班缺点: number; 新班缺点: number; 区段: HandoverReconciliation['缺点区段'] }
  '高温复采区段': HandoverReconciliation['高温复采区段']
  '补采区段': { 区段: HandoverReconciliation['补采区段']; 仍缺区段: HandoverReconciliation['补采区段']; 仍缺点数: number }
  '交接状态': string
  '是否签发': boolean
}

export async function loadHandoverSheet(id: number, role: string): Promise<HandoverSheet> {
  const response = await request(`/api/shift_handover/${id}/sheet?role=${encodeURIComponent(role)}`)
  if (!response.ok) throw new Error('交接记录读取失败')
  return await response.json()
}

export async function loadHeartbeatSummary(id: number, role: string): Promise<HeartbeatSummary> {
  const response = await request(
    `/api/device_health/shift_summary?handover_id=${id}&role=${encodeURIComponent(role)}`,
  )
  if (!response.ok) throw new Error('采集心跳汇总读取失败')
  return await response.json()
}

export async function submitHandoverEvidence(
  id: number,
  payload: { moment: string; value: number; source_type: string; role: string },
): Promise<ActionResult> {
  const response = await request(`/api/shift_handover/${id}/evidence`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await response.json()
}

export async function signOffHandover(
  id: number,
  payload: { evidence_id: string; shift: string; role: string },
): Promise<ActionResult> {
  const response = await request(`/api/shift_handover/${id}/signoff`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await response.json()
}
