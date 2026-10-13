import { useEffect, useState } from 'react'
import Chat from './Chat.jsx'
import More from './More.jsx'

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
  approvals: 'Approval cards arrive with Milestone B (the gate).',
  board: 'Goals, projects and tasks arrive with Milestone D.',
  reviews: 'Check-ins and weekly reviews arrive with Milestone D.',
}

function readTab() {
  const id = window.location.hash.replace('#', '')
  return TABS.some((t) => t.id === id) ? id : 'chat'
}

export default function App() {
  const [tab, setTab] = useState(readTab)

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
        {COMING[tab] && <p className="placeholder">{COMING[tab]}</p>}
      </main>
      <nav className="tabbar">
        {TABS.map((t) => (
          <a key={t.id} href={`#${t.id}`} className={t.id === tab ? 'tab active' : 'tab'}>
            <span className="tab-icon" aria-hidden="true">{t.icon}</span>
            <span className="tab-label">{t.label}</span>
          </a>
        ))}
      </nav>
    </div>
  )
}
