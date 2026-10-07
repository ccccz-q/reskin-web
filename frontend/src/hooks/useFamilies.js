import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'

const THREAD = 'studio'

// ★ 随仓库内置的家族（不可删除）——与后端 BUILTIN_FAMILY_IDS 保持一致
const BUILTIN_FAMILY_IDS = new Set([
  'doodle_narrators', 'epic_silhouette', 'full_restyle', 'material_pixel',
  'risograph_travel_print', 'second_world', 'split_poster',
  'surreal_collage', 'zine',
])

// 两段式删除确认的自动还原时长（毫秒）
const DEL_ARM_MS = 3500

/**
 * 家族域：启动健康检查、家族清单、选中项、两段式删除。
 *
 * ★ 为什么家族清单要单独抽一个 reload：模板工坊「安装」之后要立刻刷新，
 *   否则用户装完了还得手动刷新页面才看得到新风格。
 *
 * ★ 为什么删除要两段式：第一击武装并起3.5 秒还原定时器，再击才真删。
 *   真删会连带清理库里的安装标记，误点代价太大；弹原生 confirm 又太打断。
 */
export function useFamilies({ aliveRef, flash }) {
  const [booting, setBooting] = useState(true)
  const [health, setHealth] = useState(null)
  const [families, setFamilies] = useState([])
  const [familyId, setFamilyId] = useState('')
  const [delArm, setDelArm] = useState('')      // 当前已武装的家族 id

  const delArmTimer = useRef(null)

  const family = useMemo(
    () => families.find((f) => f.id === familyId) || null,
    [families, familyId],
  )

  // 家族清单单独抽出来：工坊安装后要立刻刷新
  const reloadFamilies = useCallback(async () => {
    try {
      const f = await api.families(true)
      if (!aliveRef.current) return
      setFamilies(f.items || [])
      return f.items || []
    } catch (e) {
      flash(`家族列表刷新失败：${e.message}`)
      return []
    }
  }, [flash, aliveRef])

  const doDeleteFamily = useCallback(async (f) => {
    try {
      const r = await api.familiesDelete(f.id)
      flash(`已删除家族「${f.name}」` +
        (r.library_unmarked > 0 ? `（库中 ${r.library_unmarked} 条安装标记已同步复位）` : ''))
      const fl = await api.families(true)
      setFamilies(fl.items || [])
      setFamilyId((cur) => (cur === f.id ? '' : cur))
    } catch (e) {
      flash(e.message || '删除失败')
    }
  }, [flash])

  // 点击删除按钮：第一击武装，第二击执行
  const onRequestDeleteFamily = useCallback((f) => {
    if (delArm === f.id) { doDeleteFamily(f); setDelArm('') }
    else {
      setDelArm(f.id)
      clearTimeout(delArmTimer.current)
      delArmTimer.current = setTimeout(() => setDelArm(''), DEL_ARM_MS)
    }
  }, [delArm, doDeleteFamily])

  // 定时器不取消就会在组件消失后触发 setState：轻则警告，
  // 重则把已经卸载的确认态又改回去（用户看到「凭空又弹一次」）
  useEffect(() => () => clearTimeout(delArmTimer.current), [])

  // ── 启动：健康检查 + 家族清单
  useEffect(() => {
    ;(async () => {
      try {
        const [h, f] = await Promise.all([api.health(THREAD), api.families(true)])
        if (!aliveRef.current) return
        setHealth(h)
        setFamilies(f.items || [])
        if (f.items?.length) setFamilyId(f.items[0].id)
      } catch (e) {
        flash(`服务暂时连不上（${e.message}）—— 请稍后刷新页面再试`)
      } finally {
        if (aliveRef.current) setBooting(false)
      }
    })()
    // ★ 作品仓库预热：启动 2s 后静默拉一次画廊列表。
    //   服务端收到请求后会后台预生成缩略图（fire-and-forget）——
    //   等用户真正点开仓库时，缩略图已在磁盘缓存里，秒开。
    const warm = setTimeout(() => { api.gallery(1, '', true).catch(() => {}) }, 2000)
    return () => clearTimeout(warm)
  }, [flash, aliveRef])

  const quota = health?.governance?.quota
  const ready = health?.ready || {}

  // 额度徽标：修复与出图都要扣，必须跟着最新policy 走
  const refreshQuota = useCallback(() => {
    api.policy(THREAD)
      .then((p) => {
        if (aliveRef.current) {
          setHealth((h) => (h ? { ...h, governance: { ...h.governance, ...p } } : h))
        }
      })
      .catch(() => {})
  }, [aliveRef])

  return {
    booting, health, families, familyId, setFamilyId, family,
    quota, ready, reloadFamilies, onRequestDeleteFamily,
    delArm, builtinIds: BUILTIN_FAMILY_IDS, refreshQuota,
  }
}