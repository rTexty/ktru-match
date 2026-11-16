import { defineConfig } from 'vite'
import { tanstackRouter } from '@tanstack/router-plugin/vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  server: {
    allowedHosts: ['.ts.net'],
    proxy: {
      // The backend serves the API under `/api` (see `app_root_path` in backend settings).
      // Trailing slash matters: bare `/api` would also shadow frontend routes like `/api-test`.
      '/api/': {
        target: process.env.VITE_DEV_PROXY_TARGET || 'http://127.0.0.1:8010',
        changeOrigin: true,
      },
    },
  },
  plugins: [
    tanstackRouter({
      target: 'react',
      semicolons: true,
      quoteStyle: 'single',
      autoCodeSplitting: true,
      routeFileIgnorePrefix: '-',
      routesDirectory: 'src/app/routes',
      generatedRouteTree: 'src/app/routeTree.gen.ts',
    }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
})
