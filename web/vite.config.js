import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

const api = process.env.KAIRO_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [
    VitePWA({
      registerType: 'prompt',     // applied on the next app open: src/design/update.js
      injectRegister: false,
      includeAssets: ['icon.svg', 'apple-touch-icon.png', 'icon-*.png', 'push-sw.js'],
      manifest: {
        name: 'Kairo',
        short_name: 'Kairo',
        description: 'Personal assistant',
        theme_color: '#09080f',
        background_color: '#09080f',
        display: 'standalone',
        orientation: 'portrait',
        start_url: '/',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icon-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // App shell only. API and websocket traffic is never cached.
        navigateFallbackDenylist: [/^\/api\//, /^\/ws\//, /^\/health/, /^\/setup\//, /^\/internal\//],
        globIgnores: ['**/splash-*.png'],   // iOS fetches the splash itself; no need to cache 1 MB
        importScripts: ['push-sw.js'],
      },
    }),
  ],
  server: {
    proxy: {
      '/api': api,
      '/health': api,
      '/ws': { target: api.replace(/^http/, 'ws'), ws: true },
    },
  },
})
