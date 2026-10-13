import { useEffect, useState } from 'react'
import Chat from './Chat.jsx'
import More from './More.jsx'
import Approvals from './Approvals.jsx'
import { api } from './api.js'

const TABS = [
  { id: 'today', label: 'Today', icon: '☀' },
  { id: 'chat', label: 'Chat', icon: '💬' },
  { id: 'approvals', label: 'Approvals', icon: '✓' },
  { id: 'board', label: 'Board', icon: '▦' },
  { id: 'reviews', label: 'Reviews', icon: '↻' },
  { id: 'more', label: 'More', icon: '⋯' },
]

const COMING = {
  today: 'Brief, top 3, calendar and deadlines arrive with Milestone C.',
  board: 'Goals, projects and tasks arrive with Milestone D.',
  reviews: 'Check-ins and weekly reviews arrive with Milestone D.',
}

function readTab() {
  const id = window.location.hash.replace('#', '')
  return TABS.some((t) => t.id === id) ? id : 'chat'
}

export default function App() {
  const [tab, setTab] = useState(readTab)
  const [openCards, setOpenCards] = useState(0)

  useEffect(() => {
    const poll = () => api('/api/approvals?status=open').then((c) => setOpenCards(c.length)).catch(() => {})
    poll()
    const t = setInterval(poll, 15000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const onHash = () => setTab(readTab())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const current = TABS.find((t) => t.id === tab)
  return (
    <div className="app">
      <header className="topbar">{current.label}</header>
      <main className="screen">
        {tab === 'chat' && <Chat />}
        {tab === 'more' && <More />}
        {tab === 'approvals' && <Approvals onCount={setOpenCards} />}
        {COMING[tab] && <p className="placeholder">{COMING[tab]}</p>}
      </main>
      <nav className="tabbar">
        {TABS.map((t) => (
          <a key={t.id} href={`#${t.id}`} className={t.id === tab ? 'tab active' : 'tab'}>
            <span className="tab-icon" aria-hidden="true">{t.icon}</span>
            {t.id === 'approvals' && openCards > 0 && <span className="count">{openCards}</span>}
            <span className="tab-label">{t.label}</span>
          </a>
        ))}
      </nav>
    </div>
  )
}
