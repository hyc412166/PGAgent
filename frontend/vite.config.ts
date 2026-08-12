import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        // Keep the dev proxy aligned with the documented local backend port.
        // VITE_API_PROXY_TARGET can still override this for another local
        // instance without changing the frontend source.
        target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
})
