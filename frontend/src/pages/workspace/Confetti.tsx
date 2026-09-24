import { useEffect, useRef } from 'react'
import { makeParticles, prefersReducedMotion, stepParticle } from './particles'

/**
 * A short, dependency-free confetti burst for the accepted-submit celebration.
 *
 * Why hand-rolled: it's ~one screen of canvas math, so a library (and its bundle
 * weight and upgrade surface) buys nothing here (DESIGN.md §2.1). Why a canvas and
 * not DOM nodes: a couple of hundred short-lived particles animate on the GPU
 * compositor cheaply, without React re-rendering per frame.
 *
 * It is decoration only, so it is inert to assistive tech (`aria-hidden`) and to
 * the pointer (`pointer-events-none`), and it renders nothing for users who ask
 * for reduced motion (see `prefersReducedMotion`).
 */

export function Confetti({ durationMs = 2400, count = 140 }: { durationMs?: number; count?: number }) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas || prefersReducedMotion()) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const width = window.innerWidth
    const height = window.innerHeight
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.scale(dpr, dpr)

    let particles = makeParticles(count, width)
    let last = performance.now()
    const start = last
    let frame = 0

    const tick = (now: number) => {
      // Cap dt so a backgrounded tab doesn't teleport the pieces when it resumes.
      const dt = Math.min((now - last) / 16, 3)
      last = now
      ctx.clearRect(0, 0, width, height)
      // Fade out over the last 30% so the burst ends softly instead of vanishing.
      const remaining = Math.max(0, 1 - (now - start) / durationMs)
      ctx.globalAlpha = Math.min(1, remaining / 0.3)
      particles = particles.map((p) => stepParticle(p, dt)).filter((p) => p.y < height + 20)
      for (const p of particles) {
        ctx.save()
        ctx.translate(p.x, p.y)
        ctx.rotate(p.rotation)
        ctx.fillStyle = p.color
        ctx.fillRect(-p.size / 2, -p.size / 4, p.size, p.size / 2)
        ctx.restore()
      }
      if (remaining > 0 && particles.length > 0) frame = requestAnimationFrame(tick)
      else ctx.clearRect(0, 0, width, height)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [durationMs, count])

  return (
    <canvas
      ref={ref}
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 z-[60] h-full w-full"
    />
  )
}
