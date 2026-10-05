import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * 为什么要配 proxy
 * ----------------
 * 旧前端把 `http://localhost:8000` 硬编码在 6 个地方，两个后果：
 *   1) 改后端端口就要全局改串
 *   2) 每个请求都跨源，绕不开 CORS 预检
 * 现在走同源 proxy：`/api` 和 `/images` 都转发给后端，前端只认相对路径。
 * 端口从 env 读（VITE_PROXY_TARGET），多人协作时不打架。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    server: {
      port: Number(env.VITE_PORT) || 5173,
      strictPort: false,
      proxy: {
        '/api': { target, changeOrigin: true },
        '/images': { target, changeOrigin: true },
      },
    },
    build: { outDir: 'dist', sourcemap: false },
  }
})
