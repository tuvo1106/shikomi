/**
 * The signed-in navigation links, in one place. The navbar renders them inline from 360px
 * up and the user menu renders the same list below that, where they don't fit in the
 * bar. Sharing the list means a new route can't be added to one and forgotten in the other.
 */
export const NAV_LINKS: { to: string; label: string }[] = [
  { to: '/problems', label: 'Problems' },
]
