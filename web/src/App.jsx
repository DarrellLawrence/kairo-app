import { AnimatePresence, MotionConfig, motion } from 'motion/react'
import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { spring, tabVariants } from './design/motion.js'
import { Badge, IconChat, IconMic, IconMore, IconShield, IconToday, TabBar, ToastProvider } from './ui/index.js'
import Today from './screens/Today.jsx'
import Chat from './screens/Chat.jsx'
import Approvals from './screens/Approvals.jsx'
import More from './screens/More.jsx'
import Reviews from './screens/Reviews.jsx'
import Board from './screens/Board.jsx'
import School from './screens/School.jsx'
import Lerndoku from './screens/Lerndoku.jsx'
import Call from './screens/Call.jsx'
import Life from './screens/Life.jsx'
import Onboarding from './screens/Onboarding.jsx'
import Loops from './screens/Loops.jsx'
import DiagVoice from './screens/DiagVoice.jsx'

const DevComponents = lazy(() => import('./screens/DevComponents.jsx'))

const TABS = [
  { id: 'today', label: 'Today', Icon: IconToday },
  { id: 'chat', label: 'Chat', Icon: IconChat },
  { id: 'approvals', label: 'Approvals', Icon: IconShield },
  { id: 'more', label: 'More', Icon: IconMore },
]
// Screens without their own tab, and the tab that stays lit while they are open.
const HIDDEN = { loops: 'today', reviews: 'more', board: 'more', school: 'more', lerndoku: 'more', life: 'more', diagvoice: 'more' }

// Routes: #today, #chat, #approvals, #reviews, #reviews/12 (push link), #more, #onboarding, #/dev/components, #/diag/voice (or /diag/voice)
function readRoute() {
  const hash = window.location.hash.replace(/^#\/?/, '')
  if (hash === 'dev/components' || window.location.pathname === '/dev/components') return 'dev'
  if (hash === 'onboarding') return 'onboarding'
  if (hash === 'diag/voice' || (!hash && window.location.pathname === '/diag/voice')) return 'diagvoice'
  if (/^call\/(\d+|new)$/.test(hash)) return 'call'
  const base = hash.split('/')[0]
  return TABS.some((t) => t.id === base) || base in HIDDEN ? base : 'today'
}

function readParam() {
  const m = window.location.hash.match(/^#\/?(?:reviews|call|loops)\/(\d+|new)$/)
  return m ? (m[1] === 'new' ? 'new' : Number(m[1])) : null
}

export default function App() {
  const [route, setRoute] = useState(readRoute)
  const [param, setParam] = useState(readParam)
  const [dir, setDir] = useState(1)
  const [openCards, setOpenCards] = useState(0)

  useEffect(() => {
    const poll = () => api('/api/approvals?status=open').then((c) => setOpenCards(c.length)).catch(() => {})
    poll()
    const t = setInterval(poll, 15000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const onHash = () => { setRoute(readRoute()); setParam(readParam()) }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = useCallback((id) => {
    const from = TABS.findIndex((t) => t.id === (HIDDEN[route] || route))
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
    today: <Today openCards={openCards} go={go} onboarding={() => { window.location.hash = '#onboarding' }} />,
    reviews: <Reviews key={param ?? 'list'} openId={param} />,
    loops: <Loops focusId={param} />,
    board: <Board />,
    school: <School />,
    lerndoku: <Lerndoku />,
    life: <Life />,
    diagvoice: <DiagVoice />,
    call: <Call key={param} callId={param} onClose={() => { window.location.hash = '#chat' }} />,
    onboarding: <Onboarding onClose={() => { window.location.hash = '#today' }} />,
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
          {route !== 'dev' && route !== 'onboarding' && route !== 'call' && <TabBar tabs={tabs} active={HIDDEN[route] || route} onSelect={go}
            onOrb={() => { window.location.hash = '#call/new' }} orbIcon={<IconMic />} />}
        </div>
      </ToastProvider>
    </MotionConfig>
  )
}
