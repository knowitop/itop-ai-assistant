import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The SPA is served by FastAPI under /ui, so assets must resolve from /ui/.
// In dev, API calls are proxied to the locally running assistant — no CORS.
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  build: {
    // Straight into the Python package: the wheel and the image both pick the
    // SPA up from there, so there is one way it reaches a deployment rather
    // than two (ADR-032). Vite refuses to clear an outDir outside its own root
    // until emptyOutDir says so explicitly.
    outDir: '../assistant/src/itop_ai_assistant/ui_dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8001',
      '/health': 'http://localhost:8001',
      '/version': 'http://localhost:8001',
    },
  },
});
