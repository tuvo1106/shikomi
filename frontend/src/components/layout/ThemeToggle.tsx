import { Moon, Sun } from 'lucide-react'
import { useTheme } from '../../lib/theme'

/** The sun/moon button that flips the theme (shows the icon of the *other* mode). */
export function ThemeToggle() {
  const { theme, toggle } = useTheme()
  return (
    <button
      onClick={toggle}
      title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-100"
    >
      {theme === 'dark' ? <Sun size={16} strokeWidth={1.5} /> : <Moon size={16} strokeWidth={1.5} />}
    </button>
  )
}
