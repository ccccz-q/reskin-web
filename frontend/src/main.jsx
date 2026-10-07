import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'

// ★ 边界放在 StrictMode **外面**：边界自己不该受开发期的双调用影响，
//   而它要兜的是整棵树 —— 任何一层渲染抛错都会先被它接住。
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
