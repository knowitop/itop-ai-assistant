import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The SPA is served by FastAPI under /ui, so assets must resolve from /ui/.
// In dev, API calls are proxied to the locally running assistant — no CORS.
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8001',
      '/health': 'http://localhost:8001',
    },
  },
});
