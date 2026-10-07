import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { orientationOf, validateUploadFile } from '../lib/upload'

/**
 * 上传域：拖拽 / 点选 / 粘贴三个入口。
 *
 * ★ 为什么要三个入口汇到同一个函数：预校验只写一次，
 *   免得哪天加第四个入口就漏掉 20MB 上限与格式白名单。
 *
 * 先本地预校验再发请求：不合格的文件连网络都不碰，
 * 也不占用上传进度与连接数（拖 20MB 照片是几十秒的事，不该白等）。
 *
 * @param deps.aliveRef 卸载哨兵：上传请求回来时组件可能已经没了
 * @param deps.setResult / setUndoStack 换原图时要把上一张成品与回退栈一起清掉
 * @param deps.workshopOpen 工坊开着时粘贴要让位（那边有自己的参考图监听）
 */
export function useUpload(deps) {
  const { aliveRef, flash, setResult, setUndoStack, workshopOpen } = deps

  const [source, setSource] = useState(null)   // {url,width,height,format,filename}
  const dropRef = useRef(null)      // 拖拽区 DOM（事件监听要挂它）
  const fileRef = useRef(null)      //隐藏的 <input type=file>

  const doUpload = useCallback(async (file) => {
    if (!file) return
    const bad = validateUploadFile(file)
    if (bad) {
      flash(bad)
      // 清空 input：否则用户修完再选同一个文件，change 不会触发（value 没变）
      if (fileRef.current) fileRef.current.value = ''
      return
    }
    try {
      const saved = await api.upload(file)
      if (!aliveRef.current) return
      setSource(saved)
      setResult(null)
      flash(`已上传 ${saved.filename}（${saved.width}×${saved.height}）`)
    } catch (e) {
      flash(e.message)
    }
  }, [flash, aliveRef, setResult])

  const clearSource = useCallback(() => {
    setSource(null)
    setResult(null)
    setUndoStack([])
    if (fileRef.current) fileRef.current.value = ''
  }, [setResult, setUndoStack])

  // 拖拽监听：不用onDragOver 是因为要给整块加 .dropping 类做高亮，
  // 而 React 的合成事件拿不到「当前拖拽是否在元素内」的可靠判断。
  useEffect(() => {
    const el = dropRef.current
    if (!el) return
    const stop = (e) => { e.preventDefault(); e.stopPropagation() }
    const over = (e) => { stop(e); el.classList.add('dropping') }
    const out = (e) => { stop(e); el.classList.remove('dropping') }
    const drop = (e) => { stop(e); el.classList.remove('dropping'); doUpload(e.dataTransfer?.files?.[0]) }
    el.addEventListener('dragover', over)
    el.addEventListener('dragleave', out)
    el.addEventListener('drop', drop)
    return () => {
      el.removeEventListener('dragover', over)
      el.removeEventListener('dragleave', out)
      el.removeEventListener('drop', drop)
    }
  }, [doUpload])

  // ── 全局粘贴上传：截图后直接 Ctrl+V，原图自动进画布。
  // 只认「图片文件」类型的粘贴项 —— 在输入框里粘贴纯文本完全不受影响。
  // 工坊打开时让位：粘贴的图进工坊参考图区（那边有自己的监听）。
  useEffect(() => {
    const onPaste = (e) => {
      if (workshopOpen) return
      const items = e.clipboardData?.items
      if (!items) return
      for (const it of items) {
        if (it.kind === 'file' && it.type.startsWith('image/')) {
          const f = it.getAsFile()
          if (f) {
            e.preventDefault()
            doUpload(f)
          }
          break
        }
      }
    }
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [doUpload, workshopOpen])

  return {
    source, setSource, dropRef, fileRef, doUpload, clearSource,
    orientation: orientationOf(source),
  }
}