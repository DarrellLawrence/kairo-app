import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

const api = process.env.KAIRO_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icon.svg', 'apple-touch-icon.png'],
      manifest: {
        name: 'Kairo',
        short_name: 'Kairo',
        description: 'Personal assistant',
        theme_color: '#111318',
        background_color: '#111318',
        display: 'standalone',
        start_url: '/',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // App shell only. API and websocket traffic is never cached.
        navigateFallbackDenylist: [/^\/api\//, /^\/ws\//, /^\/health/],
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
