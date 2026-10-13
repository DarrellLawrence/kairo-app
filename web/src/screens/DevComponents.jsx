// Hidden page: every component and animation, to check them on the iPhone.
// Open: More -> Developer -> Components, or the address #/dev/components.
import { AnimatePresence } from 'motion/react'
import { useEffect, useRef, useState } from 'react'
import {
  ApprovalCard, Badge, Button, Card, ChatBubble, EmptyState, Expandable, IconCalendar, IconInbox, IconMail, IconPlus,
  IconSparkle, IconTrash, InputBar, List, ListRow, Page, SectionTitle, Segmented, Sheet, Skeleton, SkeletonCard, Stack,
  SuccessCheck, TypingDots, useStack, useToast,
} from '../ui/index.js'

const DEMO_CARD = { id: 42, tier: 3, summary: 'Reply to Anna: "Thursday 14:00 works, see you then."', reason: 'You asked me to confirm the meeting.', args: {}, editable: [] }
const DEMO_CARD4 = { id: 43, tier: 4, summary: 'Delete event "Old dentist appointment" on 21 Oct', reason: null, args: {}, editable: [] }
const STREAM = 'Here is your afternoon: one meeting at 14:00, the report is due Friday, and nothing urgent in the inbox.'.split(/(?<= )/)

function Streaming() {
  const [chunks, setChunks] = useState(null)
  const timer = useRef(null)
  const play = () => {
    clearInterval(timer.current)
    setChunks([])
    let i = 0
    setTimeout(() => {
      timer.current = setInterval(() => {
        i += 1
        setChunks(STREAM.slice(0, i))
        if (i >= STREAM.length) clearInterval(timer.current)
      }, 90)
    }, 900)
  }
  useEffect(() => () => clearInterval(timer.current), [])
  return (
    <>
      <div className="k-messages">
        <AnimatePresence>
          <ChatBubble key="o" role="owner" text="What does my afternoon look like?" />
          {chunks && <ChatBubble key="k" role="kairo" chunks={chunks.length ? chunks : null} typing={chunks.length === 0} />}
        </AnimatePresence>
      </div>
      <div className="k-demo-row"><Button size="sm" onClick={play}>Play: typing → streaming</Button></div>
    </>
  )
}

function Pushed() {
  return (
    <Page title="Pushed page">
      <EmptyState icon={<IconSparkle />} title="iOS push / pop" text="Back button, or swipe from the left edge." />
    </Page>
  )
}

function Root() {
  const toast = useToast()
  const stack = useStack()
  const [sheet, setSheet] = useState(false)
  const [seg, setSeg] = useState('a')
  const [check, setCheck] = useState(0)
  const [rows, setRows] = useState(['Inbox zero', 'Pay rent (no, Kairo never pays)', 'Plan Saturday hike'])
  const [cards, setCards] = useState([DEMO_CARD, DEMO_CARD4])
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [count, setCount] = useState(3)

  return (
    <Page title="Components" onRefresh={() => new Promise((r) => setTimeout(r, 1200))}
      left={<Button variant="ghost" size="sm" onClick={() => { window.location.hash = '#more' }}>Close</Button>}>
      <p className="k-hello">Pull down to test pull-to-refresh. Lists stretch at both ends.</p>

      <SectionTitle>Buttons (press: scale down)</SectionTitle>
      <div className="k-demo-row">
        <Button variant="primary">Primary</Button>
        <Button>Secondary</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="danger">Danger</Button>
        <Button variant="ok" size="sm">Small</Button>
        <Button variant="primary" loading>Busy</Button>
        <Button disabled>Disabled</Button>
      </div>

      <SectionTitle>Badges (pop on change)</SectionTitle>
      <div className="k-demo-row">
        <Badge tone="accent">Sends</Badge><Badge tone="ok">OK</Badge><Badge tone="warn">Check</Badge>
        <Badge tone="danger">Deletes</Badge><Badge tone="neutral">Draft</Badge><Badge tone="count">{count}</Badge>
        <Button size="sm" onClick={() => setCount((c) => c + 1)}>+1</Button>
      </div>

      <SectionTitle>Segmented</SectionTitle>
      <div className="k-pad" style={{ marginBottom: 12 }}>
        <Segmented id="dev" value={seg} onChange={setSeg} options={[{ value: 'a', label: 'Waiting' }, { value: 'b', label: 'History' }, { value: 'c', label: 'All' }]} />
      </div>

      <SectionTitle>Cards</SectionTitle>
      <Card><p className="k-card-title">Card</p><p className="k-card-sub">Surface with a hairline and top light.</p></Card>
      <Card glass><p className="k-card-title">Glass card</p><p className="k-card-sub">Blur + translucency.</p></Card>
      <Card accent interactive><p className="k-eyebrow">Accent</p><p className="k-card-title">Interactive accent card</p><p className="k-card-sub">Press me.</p></Card>

      <SectionTitle>Shared element (tap to expand, drag the top down to close)</SectionTitle>
      <Expandable id="dev" open={open} onOpen={() => setOpen(true)} onClose={() => setOpen(false)}
        preview={<Card accent interactive><p className="k-eyebrow">Morning brief</p><p className="k-card-title">Tap to expand</p></Card>}
        detail={({ startDrag, close }) => (
          <div className="k-detail-body">
            <div className="k-detail-head" onPointerDown={startDrag}><div className="k-sheet-grab"><span /></div><p className="k-card-title">Expanded</p></div>
            <EmptyState icon={<IconSparkle />} title="Same element, now big" text="Drag the header down, or close." action={<Button onClick={close}>Close</Button>} />
          </div>
        )} />

      <SectionTitle>List rows (swipe right / left)</SectionTitle>
      <List>
        <AnimatePresence initial={false}>
          {rows.map((r) => (
            <ListRow key={r} icon={<IconInbox />} title={r} subtitle="Swipe me" chevron
              swipe={{
                right: { label: 'Done', onAction: () => { setRows((x) => x.filter((y) => y !== r)); toast.show({ text: `Done: ${r}` }) } },
                left: { label: 'Delete', onAction: () => setRows((x) => x.filter((y) => y !== r)) },
              }} />
          ))}
        </AnimatePresence>
        <ListRow icon={<IconPlus />} title="Reset rows" onClick={() => setRows(['Inbox zero', 'Pay rent (no, Kairo never pays)', 'Plan Saturday hike'])} />
        <ListRow icon={<IconCalendar />} title="Push a page" chevron onClick={() => stack.push('Pushed page', () => <Pushed />)} />
      </List>

      <SectionTitle>Approval cards (swipe right approve, left reject)</SectionTitle>
      <AnimatePresence initial={false}>
        {cards.map((c, i) => (
          <ApprovalCard key={c.id} card={c} hint={i === 0}
            onApprove={() => { setCards((x) => x.filter((y) => y.id !== c.id)); toast.show({ text: `Approved #${c.id}`, duration: 4000, action: { label: 'Undo', onAction: () => setCards((x) => [...x, c].sort((a, b) => a.id - b.id)) }, onTimeout: () => setCheck((n) => n + 1) }) }}
            onReject={() => { setCards((x) => x.filter((y) => y.id !== c.id)); toast.show({ text: `Rejected #${c.id}` }) }} />
        ))}
      </AnimatePresence>
      {cards.length < 2 && <div className="k-demo-row"><Button size="sm" onClick={() => setCards([DEMO_CARD, DEMO_CARD4])}>Reset cards</Button></div>}

      <SectionTitle>Success</SectionTitle>
      <div className="k-success"><SuccessCheck key={check} /><Button size="sm" onClick={() => setCheck((n) => n + 1)}>Replay</Button></div>

      <SectionTitle>Sheet and toast</SectionTitle>
      <div className="k-demo-row">
        <Button variant="primary" onClick={() => setSheet(true)}>Open sheet</Button>
        <Button onClick={() => toast.show({ text: 'Saved to Kairo/notes.md', icon: <IconMail /> })}>Toast</Button>
        <Button onClick={() => toast.show({ text: 'Approved #7', duration: 4000, action: { label: 'Undo', onAction: () => toast.show({ text: 'Undone' }) } })}>Toast + Undo</Button>
      </div>

      <SectionTitle>Chat</SectionTitle>
      <Streaming />
      <div className="k-messages"><ChatBubble role="system" text="System note" /><div className="k-bubble kairo"><TypingDots /></div></div>
      <div style={{ margin: '0 0 12px' }}><InputBar value={draft} onChange={setDraft} onSend={() => { toast.show({ text: `Sent: ${draft}` }); setDraft('') }} placeholder="Input bar" /></div>

      <SectionTitle>Loading (skeletons, no spinners)</SectionTitle>
      <SkeletonCard />
      <div className="k-pad"><Skeleton h={44} r={14} /></div>

      <SectionTitle>Empty state</SectionTitle>
      <EmptyState icon={<IconTrash />} title="Nothing here" text="Empty states explain what will show up and why." action={<Button size="sm">Action</Button>} />

      <Sheet open={sheet} onClose={() => setSheet(false)} title="Bottom sheet">
        <p className="k-small k-muted" style={{ textAlign: 'center' }}>Drag the handle down. A fast flick closes it; a slow small drag springs back.</p>
        <List>
          <ListRow icon={<IconMail />} title="Row one" chevron onClick={() => setSheet(false)} />
          <ListRow icon={<IconCalendar />} title="Row two" chevron onClick={() => setSheet(false)} />
        </List>
        <Button variant="primary" block onClick={() => setSheet(false)}>Close</Button>
      </Sheet>
    </Page>
  )
}

export default function DevComponents() {
  return <Stack root={<Root />} rootTitle="Components" />
}
