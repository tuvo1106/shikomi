import { Link, NavLink } from 'react-router-dom'
import type { ReactNode } from 'react'
import { Braces } from 'lucide-react'
import { useAuth } from '../../auth/session'
import { ThemeToggle } from './ThemeToggle'
import { UserMenu } from './UserMenu'
import { NAV_LINKS } from './navLinks'

function NavItem({ to, end, children }: { to: string; end?: boolean; children: ReactNode }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `flex h-full items-center border-b-2 px-1.5 text-sm transition-colors sm:px-3 ${
          isActive
            ? 'border-indigo-500 text-zinc-100'
            : 'border-transparent text-zinc-400 hover:text-zinc-100'
        }`
      }
    >
      {children}
    </NavLink>
  )
}

/**
 * The top navigation bar, present on every page. Adapts to auth state: brand +
 * nav links + user menu when signed in, brand + Sign in when logged out, and
 * always the theme toggle. Route links use `NavLink` for active highlighting.
 */
export function Navbar() {
  const { user, logout } = useAuth()
  return (
    <nav className="flex h-14 items-center justify-between gap-2 border-b border-zinc-800 px-2 sm:px-4">
      <div className="flex h-full items-center gap-2 sm:gap-6">
        <Link to="/" className="flex shrink-0 items-center gap-2">
          <span className="grid h-7 w-7 place-items-center rounded-md bg-indigo-500 text-white">
            <Braces size={16} strokeWidth={2.5} />
          </span>
          <span className="hidden font-semibold text-zinc-100 sm:block">shikomi</span>
        </Link>
        {user && (
          // Below 360px (small phones) there's no room for the links beside the brand,
          // theme toggle and avatar, so they move into the user menu (`UserMenu`) instead.
          <div className="hidden h-full items-center min-[360px]:flex">
            {NAV_LINKS.map(({ to, label }) => (
              <NavItem key={to} to={to}>
                {label}
              </NavItem>
            ))}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1 sm:gap-2">
        <ThemeToggle />
        {user ? (
          <UserMenu user={user} logout={logout} />
        ) : (
          <Link to="/login" className="px-2 text-sm text-zinc-300 hover:text-zinc-100">
            Sign in
          </Link>
        )}
      </div>
    </nav>
  )
}
