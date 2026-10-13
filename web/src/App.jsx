import { AnimatePresence, MotionConfig, motion } from 'motion/react'
import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { spring, tabVariants } from './design/motion.js'
import { Badge, IconChat, IconMore, IconShield, IconToday, TabBar, ToastProvider } from './ui/index.js'
import Today from './screens/Today.jsx'
import Chat from './screens/Chat.jsx'
import Approvals from './screens/Approvals.jsx'
import More from './screens/More.jsx'

const DevComponents = lazy(() => import('./screens/DevComponents.jsx'))

// Board and Reviews join the tab bar when they are built (Milestone D).
const TABS = [
  { id: 'today', label: 'Today', Icon: IconToday },
  { id: 'chat', label: 'Chat', Icon: IconChat },
  { id: 'approvals', label: 'Approvals', Icon: IconShield },
  { id: 'more', label: 'More', Icon: IconMore },
]

function readRoute() {
  const hash = window.location.hash.replace(/^#\/?/, '')
  if (hash === 'dev/components' || window.location.pathname === '/dev/components') return 'dev'
  return TABS.some((t) => t.id === hash) ? hash : 'today'
}

export default function App() {
  const [route, setRoute] = useState(readRoute)
  const [dir, setDir] = useState(1)
  const [openCards, setOpenCards] = useState(0)

  useEffect(() => {
    const poll = () => api('/api/approvals?status=open').then((c) => setOpenCards(c.length)).catch(() => {})
    poll()
    const t = setInterval(poll, 15000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const onHash = () => setRoute(readRoute())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = useCallback((id) => {
    const from = TABS.findIndex((t) => t.id === route)
    const to = TABS.findIndex((t) => t.id === id)
    setDir(to >= from ? 1 : -1)
    window.history.replaceState(null, '', `#${id}`)
    setRoute(id)
  }, [route])

  const tabs = TABS.map((t) => ({
    id: t.id, label: t.label, icon: <t.Icon />,
    badge: t.id === 'approvals' && openCards > 0 ? <Badge tone="count">{openCards}</Badge> : null,
  }))

  const screen = {
    today: <Today openCards={openCards} go={go} />,
    chat: <Chat />,
    approvals: <Approvals onCount={setOpenCards} />,
    more: <More />,
    dev: <Suspense fallback={null}><DevComponents /></Suspense>,
  }[route]

  return (
    <MotionConfig reducedMotion="user">
      <ToastProvider>
        <div className="k-app">
          <main className="k-screen">
            <AnimatePresence initial={false} custom={dir} mode="popLayout">
              <motion.div
                key={route}
                className="k-layer"
                custom={dir}
                variants={tabVariants}
                initial="enter"
                animate="center"
                exit="exit"
                transition={{ ...spring.smooth, opacity: { duration: 0.18 } }}
              >
                {screen}
              </motion.div>
            </AnimatePresence>
          </main>
          {route !== 'dev' && <TabBar tabs={tabs} active={route} onSelect={go} />}
        </div>
      </ToastProvider>
    </MotionConfig>
  )
}
