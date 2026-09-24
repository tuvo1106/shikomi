import { useEffect, useRef, useState } from 'react'
import { Link, NavLink } from 'react-router-dom'
import { ChevronDown, LogOut, Settings } from 'lucide-react'
import type { User } from '../../api/types'
import { NAV_LINKS } from './navLinks'

/**
 * The nav's account dropdown (avatar + username → menu with Sign out). Closes on
 * click-outside and Escape via a document listener that's only attached while
 * open, and torn down on close — no leaked global handlers.
 */
export function UserMenu({ user, logout }: { user: User; logout: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        // Below `sm` the button is just an initial and a chevron (username hidden), so it
        // needs its own accessible name or a screen reader says "D, button".
        aria-label={`Account menu, ${user.username}`}
        aria-expanded={open}
        className="flex items-center gap-1.5 rounded-md py-1 pl-1 pr-1.5 text-sm text-zinc-300 hover:bg-zinc-800/60"
      >
        <span className="grid h-6 w-6 place-items-center rounded-full bg-indigo-500/20 text-xs font-semibold text-indigo-300">
          {user.username.slice(0, 1).toUpperCase()}
        </span>
        <span className="hidden max-w-[8rem] truncate sm:block">{user.username}</span>
        <ChevronDown size={14} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-52 overflow-hidden rounded-md border border-zinc-800 bg-zinc-900 py-1 shadow-lg shadow-black/30">
          <div className="px-3 py-2 text-xs text-zinc-500">
            Signed in as
            <div className="truncate text-sm font-medium text-zinc-200">{user.username}</div>
          </div>
          <div className="my-1 border-t border-zinc-800" />
          {/* The navbar's links, shown here only below 360px where they don't fit in the bar. */}
          <div data-testid="menu-nav-links" className="min-[360px]:hidden">
            {NAV_LINKS.map(({ to, label }) => (
              <NavLink
                key={to}
                to={to}
                onClick={() => setOpen(false)}
                // NavLink sets aria-current on the active route, matching the bar's cue.
                className={({ isActive }) =>
                  `flex w-full items-center px-3 py-1.5 text-left text-sm hover:bg-zinc-800 ${
                    isActive ? 'text-zinc-100' : 'text-zinc-300'
                  }`
                }
              >
                {label}
              </NavLink>
            ))}
            <div className="my-1 border-t border-zinc-800" />
          </div>
          <Link
            to="/settings"
            onClick={() => setOpen(false)}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-zinc-300 hover:bg-zinc-800"
          >
            <Settings size={14} strokeWidth={1.5} /> Settings
          </Link>
          <div className="my-1 border-t border-zinc-800" />
          <button
            onClick={() => {
              setOpen(false)
              logout()
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-zinc-300 hover:bg-zinc-800"
          >
            <LogOut size={14} strokeWidth={1.5} /> Sign out
          </button>
        </div>
      )}
    </div>
  )
}
