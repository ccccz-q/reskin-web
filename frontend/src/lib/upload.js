// 上传域的纯逻辑 —— 从 App.jsx 原样搬出，一个字都没改。
//
// 为什么要前端先拦：20MB 上限原本只写在拖拽区那行提示文案里，
// 用户要等整张图传完、服务端回 413 才知道自己超了 —— 白等一趟。

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024          // = backend MAX_UPLOAD_BYTES
const ACCEPTED_EXT = /\.(jpe?g|png|webp)$/i        // = backend ALLOWED_IMAGE_FORMATS（JPEG/PNG/WEBP）
// 部分环境（截图工具、Word 粘贴）不给扩展名，只给 MIME —— 所以两条路都要认
const ACCEPTED_MIME = /^image\/(jpeg|png|webp)$/

/** 返回错误文案；null 表示这份文件可以发。 */
export function validateUploadFile(file) {
  if (!file) return null
  const name = file.name || '这个文件'
  // 先看类型再看大小：把「选错文件」报成「文件太大」会把人引到错误方向
  if (!(ACCEPTED_MIME.test(file.type) || ACCEPTED_EXT.test(name))) {
    return `「${name}」不是图片 —— 请上传 JPG / PNG / WebP 格式的照片`
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    // 用 MB 而不是字节数：用户看到「20971520」没有任何判断力
    return `「${name}」有 ${(file.size / 1024 / 1024).toFixed(1)}MB，超过 20MB 上限 —— 请先压缩或换一张`
  }
  return null
}

/** 原图朝向，给缩略图下方一句人话标注。 */
export function orientationOf(source) {
  if (!source) return null
  if (source.height > source.width) return '竖构图'
  if (source.width > source.height) return '横构图'
  return '方构图'
}