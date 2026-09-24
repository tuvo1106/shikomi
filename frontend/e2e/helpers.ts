import { execFileSync } from 'node:child_process'
import { createHmac } from 'node:crypto'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { expect, type Page } from '@playwright/test'

export const DEV_USER = { email: 'dev@example.com', password: 'devpassword' }

// The solve flow runs against a bundled starter problem, read straight from its seed
// file (cwd is frontend/ when Playwright runs) so the spec can't drift from what
// `app.cli seed` loaded. Its first editorial solution is the correct submission; the
// starter code, whose methods all return None, is the wrong one. Code is injected via
// localStorage (`code:<slug>`, which the Workspace reads on mount) so we never have to
// fight Monaco's auto-indent when typing Python.
const SOLVE_SEED = JSON.parse(
  readFileSync(path.resolve(process.cwd(), '../seed/problems/design-vending-machine.json'), 'utf8'),
)
export const SOLVE_PROBLEM: { slug: string; title: string } = {
  slug: SOLVE_SEED.slug,
  title: SOLVE_SEED.title,
}
export const SOLVE_CORRECT: string = SOLVE_SEED.solutions[0].code
export const SOLVE_WRONG: string = SOLVE_SEED.starter_code

function unique(prefix: string) {
  return `${prefix}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
}

/** Register a fresh user through the UI. New accounts are unverified, so this
 * lands on the email-verification gate (not the app). */
export async function registerNewUser(page: Page) {
  const creds = {
    email: `${unique('e2e-')}@example.com`,
    username: unique('e2e_'),
    password: 'sunflower-desk-42',
  }
  await page.goto('/register')
  await page.getByLabel('Email').fill(creds.email)
  await page.getByLabel('Username').fill(creds.username)
  await page.getByLabel(/Password/).fill(creds.password)
  await page.getByRole('button', { name: 'Register' }).click()
  await expect(page.getByRole('heading', { name: /Check your email/i })).toBeVisible()
  return creds
}

/** Sign in through the UI (used for the seeded dev user). */
export async function login(page: Page, email: string, password: string) {
  await page.goto('/login')
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()
}

/** Seed editor code for a slug, then open that problem's workspace. */
export async function openProblemWithCode(page: Page, slug: string, code: string) {
  await page.evaluate(([k, v]) => localStorage.setItem(k, v), [`code:${slug}`, code] as const)
  await page.goto(`/problems/${slug}`)
}

/** Mark an account's email verified via the ops CLI (there's no inbox to click a link in). */
export function verifyEmailViaCli(email: string) {
  // cwd is frontend/ when Playwright runs; the CLI needs the backend's project + env.
  execFileSync('uv', ['run', 'python', '-m', 'app.cli', 'verify-email', email], {
    cwd: path.resolve(process.cwd(), '../backend'),
    stdio: 'pipe',
  })
}

const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

/** RFC 6238 code (SHA1, 6 digits, 30s) for a base32 secret, `offset` steps from now. */
export function totpCode(secret: string, offset = 0) {
  let bits = ''
  for (const ch of secret.replace(/[\s=]/g, '').toUpperCase()) bits += B32.indexOf(ch).toString(2).padStart(5, '0')
  const key = Buffer.from(bits.match(/.{8}/g)!.map((b) => parseInt(b, 2)))
  const counter = Buffer.alloc(8)
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000) + offset))
  const h = createHmac('sha1', key).update(counter).digest()
  const o = h[h.length - 1] & 0xf
  const n = ((h[o] & 0x7f) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3]
  return String(n % 1_000_000).padStart(6, '0')
}
