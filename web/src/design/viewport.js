// iOS standalone app height. Normally the app is `position: fixed; inset: 0`,
// which covers the whole screen down to the home indicator (viewport-fit=cover).
// A measured height is only used while the keyboard is open, so the input bar
// sits right on the keyboard. (Measuring all the time left a black band under
// the tab bar: in standalone mode the visual viewport can be shorter than the
// screen by the bottom safe area.)
export function trackViewport() {
  const vv = window.visualViewport
  const root = document.documentElement
  const apply = () => {
    const keyboard = vv && window.innerHeight - vv.height > 120
    root.dataset.keyboard = keyboard ? '1' : '0'
    if (keyboard) root.style.setProperty('--app-h', `${Math.round(vv.height)}px`)
    else root.style.removeProperty('--app-h')
    root.style.setProperty('--kb-safe', keyboard ? '0px' : 'env(safe-area-inset-bottom, 0px)')
    // iOS scrolls the page to show the focused field; the app never moves.
    if (window.scrollY !== 0) window.scrollTo(0, 0)
  }
  apply()
  vv?.addEventListener('resize', apply)
  vv?.addEventListener('scroll', apply)
  window.addEventListener('resize', apply)
}
