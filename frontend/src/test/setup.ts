import '@testing-library/jest-dom'
import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

// jsdom lacks ResizeObserver (react-resizable-panels) and a working localStorage
// in this environment. Provide fresh stubs before each test.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  // Start each test with no session-hint cookie (see client.ts refreshSession).
  document.cookie = 'has_session=; Max-Age=0; path=/'
  // jsdom has no matchMedia (used by useMediaQuery / theme detection).
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
    onchange: null,
  }))
  // jsdom has no canvas: getContext logs a "not implemented" error. Return null, which
  // is also what real browsers do when a context is unavailable (Confetti bails on it).
  vi.stubGlobal('HTMLCanvasElement', class extends HTMLCanvasElement {})
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
