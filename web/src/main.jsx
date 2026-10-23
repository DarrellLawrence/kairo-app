import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './design/tokens.css'
import './design/base.css'
import { trackViewport } from './design/viewport.js'
import { keepFresh } from './design/update.js'
import { keepFlags } from './flags.js'

trackViewport()
keepFresh()
keepFlags()   // the owner's switches (More -> System -> Experiments), read again whenever the app comes back

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
