// Line icons (SF Symbols style), 24x24, stroke = currentColor. No icon library.
const P = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }

const make = (paths) => function Icon(props) {
  return <svg viewBox="0 0 24 24" aria-hidden="true" {...P} {...props}>{paths}</svg>
}

export const IconToday = make(<><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>)
export const IconChat = make(<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.6A8 8 0 1 1 21 12Z" />)
export const IconCheck = make(<path d="M5 12.5l4.2 4.2L19 7" />)
export const IconShield = make(<><path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3Z" /><path d="M9 12l2 2 4-4" /></>)
export const IconBoard = make(<><rect x="3" y="4" width="7" height="16" rx="2" /><rect x="14" y="4" width="7" height="10" rx="2" /></>)
export const IconReview = make(<><path d="M20 12a8 8 0 1 1-2.3-5.7" /><path d="M20 4v5h-5" /></>)
export const IconMore = make(<><circle cx="5" cy="12" r="1.3" /><circle cx="12" cy="12" r="1.3" /><circle cx="19" cy="12" r="1.3" /></>)
export const IconChevron = make(<path d="M9 5l7 7-7 7" />)
export const IconBack = make(<path d="M15 5l-7 7 7 7" />)
export const IconSend = make(<><path d="M12 19V5" /><path d="M6 11l6-6 6 6" /></>)
export const IconPlus = make(<path d="M12 5v14M5 12h14" />)
export const IconX = make(<path d="M6 6l12 12M18 6L6 18" />)
export const IconMail = make(<><rect x="3" y="5" width="18" height="14" rx="3" /><path d="M4 7l8 6 8-6" /></>)
export const IconCalendar = make(<><rect x="3" y="5" width="18" height="16" rx="3" /><path d="M3 10h18M8 3v4M16 3v4" /></>)
export const IconBell = make(<><path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15L6 16Z" /><path d="M10 20a2 2 0 0 0 4 0" /></>)
export const IconPower = make(<><path d="M12 3v8" /><path d="M6.3 7a8 8 0 1 0 11.4 0" /></>)
export const IconPulse = make(<path d="M3 12h4l2-5 4 10 2-5h6" />)
export const IconList = make(<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" />)
export const IconSparkle = make(<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3ZM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16Z" />)
export const IconInbox = make(<><path d="M3 13l3-8h12l3 8v6H3v-6Z" /><path d="M3 13h5l1 2h6l1-2h5" /></>)
export const IconKey = make(<><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M17 6l3 3" /></>)
export const IconArrowDown = make(<path d="M12 5v14M6 13l6 6 6-6" />)
export const IconRefresh = make(<><path d="M20 12a8 8 0 1 1-2.3-5.7" /><path d="M20 4v5h-5" /></>)
export const IconWarn = make(<><path d="M12 3l10 18H2L12 3Z" /><path d="M12 10v4M12 17.5h.01" /></>)
export const IconTrash = make(<><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" /></>)
export const IconPin = make(<><path d="M12 21s-6-5.6-6-11a6 6 0 1 1 12 0c0 5.4-6 11-6 11Z" /><circle cx="12" cy="10" r="2.2" /></>)
export const IconTrain = make(<><rect x="5" y="3" width="14" height="14" rx="3" /><path d="M5 11h14M9 21l1.5-4M15 21l-1.5-4" /><circle cx="9" cy="14" r=".6" /><circle cx="15" cy="14" r=".6" /></>)
export const IconCloud = make(<path d="M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9.5 4.3 4.3 0 0 1 17.5 18H7Z" />)
export const IconMic = make(<><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" /></>)
export const IconSpeaker = make(<><path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4v-5Z" /><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" /></>)
export const IconPhone = make(<path d="M6.6 3.5h2.6l1.4 4-2 1.4a12 12 0 0 0 6.5 6.5l1.4-2 4 1.4v2.6a2 2 0 0 1-2.2 2A17 17 0 0 1 4.6 5.7a2 2 0 0 1 2-2.2Z" />)
export const IconCaptions = make(<><rect x="3" y="5" width="18" height="14" rx="3" /><path d="M10 10.5a2 2 0 1 0 0 3M17 10.5a2 2 0 1 0 0 3" /></>)
export const IconPause = make(<path d="M9 5v14M15 5v14" />)
export const IconGear = make(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" /></>)
export const IconHeart = make(<path d="M12 20s-7.5-4.6-9.3-9.2C1.4 7.4 3.6 4 7 4c2 0 3.4 1.1 5 3 1.6-1.9 3-3 5-3 3.4 0 5.6 3.4 4.3 6.8C19.5 15.4 12 20 12 20Z" />)
export const IconChart = make(<path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />)
export const IconBolt = make(<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8Z" />)
export const IconMoon = make(<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />)

/* The streak flame: a soft 3D glassy icon (hero element, not a line icon). */
export function Flame3D(props) {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true" {...props}>
      <defs>
        <radialGradient id="kf-outer" cx="50%" cy="78%" r="70%">
          <stop offset="0" stopColor="#ffd9a8" />
          <stop offset="0.35" stopColor="#ff8a3d" />
          <stop offset="0.75" stopColor="#ff3d2e" />
          <stop offset="1" stopColor="#b8211a" />
        </radialGradient>
        <radialGradient id="kf-inner" cx="50%" cy="70%" r="60%">
          <stop offset="0" stopColor="#ffffff" />
          <stop offset="0.5" stopColor="#fff1dc" />
          <stop offset="1" stopColor="#ffc685" />
        </radialGradient>
        <linearGradient id="kf-shine" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fff" stopOpacity="0.7" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d="M33 4c2 9 13 15 13 30a14 14 0 0 1-28 0c0-7 3-11 6-14 0 5 2 8 5 9-2-9 1-18 4-25Z" fill="url(#kf-outer)" />
      <path d="M32 30c1 5 7 8 7 15a7 7 0 0 1-14 0c0-5 4-8 7-15Z" fill="url(#kf-inner)" />
      <path d="M30 9c-2 5-4 9-4 14 2-4 5-7 7-9-1-2-2-3-3-5Z" fill="url(#kf-shine)" opacity="0.6" />
    </svg>
  )
}
