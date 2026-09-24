import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import { Confetti } from './Confetti'
import { makeParticles, prefersReducedMotion, stepParticle } from './particles'

describe('confetti particles', () => {
  it('spawns the requested count above the viewport, spread around the centre', () => {
    const ps = makeParticles(30, 1000, () => 0.5)
    expect(ps).toHaveLength(30)
    expect(ps.every((p) => p.y < 0 && p.x === 500)).toBe(true)
  })

  it('falls under gravity while drag slows the sideways drift', () => {
    const [p] = makeParticles(1, 1000, () => 1)
    const next = stepParticle(p, 1)
    expect(next.y).toBeGreaterThan(p.y)
    expect(next.vy).toBeGreaterThan(p.vy)
    expect(Math.abs(next.vx)).toBeLessThan(Math.abs(p.vx))
  })
})

describe('reduced motion', () => {
  const stubMedia = (matches: boolean) =>
    vi.stubGlobal('matchMedia', (query: string) => ({ matches, media: query }))

  it('is detected from the media query', () => {
    stubMedia(true)
    expect(prefersReducedMotion()).toBe(true)
    stubMedia(false)
    expect(prefersReducedMotion()).toBe(false)
  })

  it('stays off when matchMedia is unavailable', () => {
    vi.stubGlobal('matchMedia', undefined)
    expect(prefersReducedMotion()).toBe(true)
  })

  it('draws nothing (no animation frame requested) when the user prefers reduced motion', () => {
    stubMedia(true)
    const raf = vi.spyOn(window, 'requestAnimationFrame')
    render(<Confetti />)
    expect(raf).not.toHaveBeenCalled()
  })

  it('is hidden from assistive tech and never intercepts clicks', () => {
    stubMedia(true)
    const { container } = render(<Confetti />)
    const canvas = container.querySelector('canvas')!
    expect(canvas).toHaveAttribute('aria-hidden', 'true')
    expect(canvas.className).toContain('pointer-events-none')
  })
})
