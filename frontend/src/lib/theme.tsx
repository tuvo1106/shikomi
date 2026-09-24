/**
 * Light/dark theme state. The actual color flip is pure CSS — toggling a `light`
 * class on `<html>` swaps the inverted-zinc CSS variables (see index.css) — so
 * this module only tracks the choice, persists it, and applies that one class.
 */
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type Theme = 'dark' | 'light'

const KEY = 'theme'

/** Initial theme: a saved choice wins, else follow the OS `prefers-color-scheme`. */
function initial(): Theme {
  const saved = localStorage.getItem(KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

// Default value is only used if a consumer renders outside the provider; the real
// value comes from <ThemeProvider>.
const ThemeContext = createContext<{ theme: Theme; toggle: () => void }>({
  theme: 'dark',
  toggle: () => {},
})

/** Tracks the theme, syncs the `<html>` class + localStorage whenever it changes. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(initial)

  useEffect(() => {
    document.documentElement.classList.toggle('light', theme === 'light')
    localStorage.setItem(KEY, theme)
  }, [theme])

  const toggle = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))
  return <ThemeContext.Provider value={{ theme, toggle }}>{children}</ThemeContext.Provider>
}

/** Read `{ theme, toggle }`. */
// eslint-disable-next-line react-refresh/only-export-components
export const useTheme = () => useContext(ThemeContext)
