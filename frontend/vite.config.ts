/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Proxy API calls to the backend in dev so the app is same-origin (cookies work).
    proxy: { '/api': 'http://localhost:8000' },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    // Only unit/component tests under src/; e2e/ is Playwright's (see playwright.config.ts).
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})
