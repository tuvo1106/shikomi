/** Pure particle math and motion preference for `Confetti` (kept out of the component
 * file so it can be unit-tested and so fast refresh keeps working). */

const COLORS = ['#818cf8', '#34d399', '#fbbf24', '#f472b6', '#60a5fa', '#f87171']

export type Particle = {
  x: number
  y: number
  vx: number
  vy: number
  size: number
  color: string
  rotation: number
  spin: number
}

/** Whether the user has asked the OS/browser for reduced motion. Safe where
 *  `matchMedia` is missing (older browsers, some test environments): motion stays off. */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return true
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** Spawn `count` particles in a burst from the top-centre, fanning out and downward.
 *  `rand` is injectable so the spread is deterministic under test. */
export function makeParticles(count: number, width: number, rand: () => number = Math.random): Particle[] {
  return Array.from({ length: count }, (_, i) => ({
    x: width / 2 + (rand() - 0.5) * width * 0.3,
    y: -10,
    vx: (rand() - 0.5) * 10,
    vy: rand() * 4 + 2,
    size: rand() * 6 + 4,
    color: COLORS[i % COLORS.length],
    rotation: rand() * Math.PI * 2,
    spin: (rand() - 0.5) * 0.3,
  }))
}

/** Advance one particle by `dt` frames (1 ≈ 16ms): gravity pulls down, air drag
 *  slows the sideways drift, and the piece tumbles. Returns a new particle. */
export function stepParticle(p: Particle, dt: number): Particle {
  return {
    ...p,
    x: p.x + p.vx * dt,
    y: p.y + p.vy * dt,
    vx: p.vx * Math.pow(0.99, dt),
    vy: p.vy + 0.12 * dt,
    rotation: p.rotation + p.spin * dt,
  }
}
