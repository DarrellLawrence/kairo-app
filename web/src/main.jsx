import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './design/tokens.css'
import './design/base.css'
import { trackViewport } from './design/viewport.js'
import { keepFresh } from './design/update.js'

trackViewport()
keepFresh()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
