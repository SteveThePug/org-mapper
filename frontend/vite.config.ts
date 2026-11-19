import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the API runs on :8000; proxying keeps the session cookie same-origin,
// exactly like production where Caddy serves both behind one host.
export default defineConfig({
  plugins: [react()],
  // Internal tool behind a login: ~200 kB gzipped per chunk is acceptable.
  build: { chunkSizeWarningLimit: 700 },
  server: {
    proxy: { '/api': 'http://localhost:8000' },
  },
})
