import { NAV_ITEMS, ZONE_META, type NavZone, useRole } from '../role'

/**
 * 左侧分组导航：三区（我要做的事 / 系统里的资产 / 治理与运营）。
 *
 *  · 按当前角色过滤：access==='hidden' 的模块直接不渲染（看不到 = 最干净的隔离）。
 *  · access==='readonly' 的模块带「只读」标记，点击仍可进入查看，但页内操作会被置灰。
 *  · access==='disabled' 的模块出现但不可点（预留：未来某些角色可见不可入）。
 */
export default function SideNav({
  active,
  onSelect,
}: {
  /** 当前选中的模块 key（菜单 key） */
  active: string
  onSelect: (key: string) => void
}) {
  const { role, stateOf } = useRole()
  const zones: NavZone[] = ['do', 'assets', 'govern']

  return (
    <nav className="sidenav" aria-label="主导航">
      <div className="sidenav-role">
        <span className="sidenav-role-tag">{role}</span>
        <span className="sidenav-role-name">{/* 角色名由上层标题区统一呈现，这里只放标识 */}</span>
      </div>
      {zones.map((z) => {
        const items = NAV_ITEMS.filter((n) => n.zone === z && stateOf(n.key) !== 'hidden')
        if (items.length === 0) return null
        return (
          <div className="navzone" key={z}>
            <div className="navzone-title">{ZONE_META[z]}</div>
            <ul className="navlist">
              {items.map((it) => {
                const st = stateOf(it.key)
                const isActive = active === it.key
                return (
                  <li key={it.key}>
                    <button
                      type="button"
                      className={`navitem${isActive ? ' active' : ''}`}
                      onClick={() => onSelect(it.key)}
                      title={it.desc}
                      aria-current={isActive ? 'page' : undefined}
                      disabled={st === 'disabled'}
                    >
                      <span className="navitem-label">{it.label}</span>
                      {st === 'readonly' ? <span className="navtag">只读</span> : null}
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </nav>
  )
}
