import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'

/**
 * 角色与权限模型（前端 UX 层，仅做交互隔离；BFF 侧强制复核）。
 *
 * 设计铁律：
 *  · 前端是「不可信」的——这里隐藏/置灰的交互元素，BFF 仍会按 scope 再校验一遍。
 *  · 四态访问：full（可编辑）/ readonly（仅查看）/ hidden（不出现）/ disabled（出现但不可点）。
 *  · 当前工程零鉴权（actor 自由文本透传），故角色为本地演示开关；
 *    SSO 就绪后由 `/me` 解析，本文件只换数据源、不动契约。
 */

export type Role = 'R1' | 'R2' | 'R3' | 'R4'
export type AccessState = 'full' | 'readonly' | 'hidden' | 'disabled'

export const ROLE_META: Record<Role, { label: string; short: string; desc: string }> = {
  R1: { label: '业务需求方', short: '业务方', desc: '提需求、确认口径、看结果' },
  R2: { label: '需求分析师', short: '分析师', desc: '解析语义、写 SQL、联调交付' },
  R3: { label: '数据管家', short: '数据管家', desc: '维护字典、经验库与取数通道' },
  R4: { label: '系统管理员', short: '管理员', desc: '系统配置与审计回放' },
}

export const ROLES: Role[] = ['R1', 'R2', 'R3', 'R4']

export type NavZone = 'do' | 'assets' | 'govern'

export interface NavItem {
  key: string
  label: string
  zone: NavZone
  desc: string
  /** 各角色对该模块的访问态；缺省视为 full */
  access: Record<Role, AccessState>
}

/**
 * 导航资产（= 前端 need ↔ BFF scope 契约的静态投影）。
 * 业务术语按《UI 优化方案 v1.0》权威表：业务说法字典 / 经验库 / 取数通道 /
 * 复盘留痕 / 数据空间。
 */
export const NAV_ITEMS: NavItem[] = [
  // 我要做的事：需求全链路
  {
    key: 'demand',
    label: '需求链路',
    zone: 'do',
    desc: '从提需求到出数交付的一条龙',
    access: { R1: 'full', R2: 'full', R3: 'readonly', R4: 'readonly' },
  },
  // 系统里的资产
  {
    key: 'mdl',
    label: '业务说法字典',
    zone: 'assets',
    desc: '业务术语到库表字段的映射',
    access: { R1: 'readonly', R2: 'readonly', R3: 'full', R4: 'readonly' },
  },
  {
    key: 'knowledge',
    label: '经验库',
    zone: 'assets',
    desc: '沉淀过的口径与判断经验',
    access: { R1: 'readonly', R2: 'readonly', R3: 'full', R4: 'readonly' },
  },
  {
    key: 'datasource',
    label: '取数通道',
    zone: 'assets',
    desc: '已接入的数据源与表',
    access: { R1: 'readonly', R2: 'readonly', R3: 'full', R4: 'readonly' },
  },
  // 治理与运营
  {
    key: 'audit',
    label: '复盘留痕',
    zone: 'govern',
    desc: '完整判断链回放，谁在何时改了什么',
    access: { R1: 'hidden', R2: 'readonly', R3: 'readonly', R4: 'full' },
  },
  {
    key: 'system',
    label: '系统设置',
    zone: 'govern',
    desc: '数据空间与全局配置',
    access: { R1: 'hidden', R2: 'hidden', R3: 'readonly', R4: 'full' },
  },
]

export const ZONE_META: Record<NavZone, string> = {
  do: '我要做的事',
  assets: '系统里的资产',
  govern: '治理与运营',
}

export interface RoleCtx {
  role: Role
  setRole: (r: Role) => void
  /** 当前角色下某模块的访问态（找不到默认 full） */
  stateOf: (key: string) => AccessState
  /** 当前角色对该模块是否只读 */
  isReadonly: (key: string) => boolean
  /** 提交留痕用的身份标识（SSO 就绪前用角色身份占位） */
  actor: string
}

const Ctx = createContext<RoleCtx | null>(null)
const ROLE_KEY = 'askoda.role'

export function RoleProvider({ children }: { children: ReactNode }) {
  const [role, setRoleState] = useState<Role>(() => {
    try {
      const v = window.localStorage.getItem(ROLE_KEY) as Role | null
      if (v && ROLES.includes(v)) return v
    } catch {
      /* 隐私模式：回落默认角色 */
    }
    return 'R2'
  })

  const setRole = (r: Role) => {
    try {
      window.localStorage.setItem(ROLE_KEY, r)
    } catch {
      /* 忽略 */
    }
    setRoleState(r)
  }

  const value = useMemo<RoleCtx>(() => {
    const find = (k: string) => NAV_ITEMS.find((n) => n.key === k)
    const stateOf = (k: string): AccessState => {
      const it = find(k)
      return it ? it.access[role] : 'full'
    }
    return {
      role,
      setRole,
      stateOf,
      isReadonly: (k: string) => stateOf(k) === 'readonly',
      actor: `${role}-${ROLE_META[role].short}`,
    }
  }, [role])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useRole(): RoleCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error('useRole 必须在 <RoleProvider> 内使用')
  return c
}
