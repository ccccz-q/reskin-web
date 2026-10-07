// 下载相关的纯逻辑 —— 从 App.jsx 原样搬出。

/**
 * 从 blob 的 MIME 推文件扩展名。
 * jpeg → jpg：用户不认.jpeg 这个后缀，也不想在文件名里看到它。
 * 拿不到子类型时回落 jpg —— 成品图基本都是 jpg/png，猜错顶多打不开，
 * 比弹一个「.undefined」的文件名体面得多。
 */
export function extFromMime(type) {
  return (String(type || '').split('/')[1] || 'jpg').replace('jpeg', 'jpg')
}

/** 下载文件名：travelnote-YYYY-MM-DD.jpg。同一天多次下载会覆盖，故加序号。 */
export function downloadName(type, seq = 0) {
  const day = new Date().toISOString().slice(0, 10)
  const suffix = seq > 0 ? `-${seq}` : ''
  return `travelnote-${day}${suffix}.${extFromMime(type)}`
}

/**
 * 打包下载的文件名。★为什么要带计数：同一天既下载过单张、又下载过打包时，
 * 两次都叫「作品-2026-10-07.zip」，浏览器会把它当同一个文件。
 */
export function zipDownloadName(seq = 0) {
  const day = new Date().toISOString().slice(0, 10)
  return seq > 0 ? `作品-${day}-${seq}.zip` : `作品-${day}.zip`
}

/** 选中 1 张走原图直下，多张才值得打包 —— 省掉后端一次 zip 合成。 */
export function downloadModeFor(count) {
  if (count === 0) return 'none'
  return count === 1 ? 'single' : 'zip'
}