// iOS standalone app height: follows the visual viewport, so when the keyboard
// opens the app shrinks and the input bar stays right above the keyboard.
export function trackViewport() {
  const vv = window.visualViewport
  const root = document.documentElement
  const apply = () => {
    const h = vv ? vv.height : window.innerHeight
    root.style.setProperty('--app-h', `${Math.round(h)}px`)
    // Keyboard open: no home-bar gap under the input bar.
    const keyboard = vv && window.innerHeight - vv.height > 120
    root.style.setProperty('--kb-safe', keyboard ? '0px' : 'env(safe-area-inset-bottom, 0px)')
    root.dataset.keyboard = keyboard ? '1' : '0'
    // iOS scrolls the page to show the focused field; the app never moves.
    if (window.scrollY !== 0) window.scrollTo(0, 0)
  }
  apply()
  vv?.addEventListener('resize', apply)
  vv?.addEventListener('scroll', apply)
  window.addEventListener('resize', apply)
}
