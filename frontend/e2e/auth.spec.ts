import { test, expect } from '@playwright/test'
import { registerNewUser, SOLVE_PROBLEM } from './helpers'

test('the root sends a signed-out visitor to sign in, with a way to register', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login$/)
  await page.getByRole('link', { name: 'Register' }).click()
  await expect(page).toHaveURL(/\/register$/)
})

test('registration asks you to confirm your email before signing in', async ({ page }) => {
  const creds = await registerNewUser(page)
  // Registering doesn't sign you in: the account can't log in until verified.
  await expect(page.getByRole('link', { name: SOLVE_PROBLEM.title })).toHaveCount(0)

  // And signing in before verifying gets the same answer as a wrong password.
  await page.getByRole('link', { name: 'Go to sign in' }).click()
  await page.getByLabel('Email').fill(creds.email)
  await page.getByLabel('Password').fill(creds.password)
  await page.getByRole('button', { name: /Sign in/i }).click()
  await expect(page.getByText(/Just signed up\? Confirm your email first/)).toBeVisible()

  // …and the login page offers a fresh link, since there's no session to ask from.
  await page.getByRole('button', { name: /Resend it/i }).click()
  await expect(page.getByText(/a new link is on its way/i)).toBeVisible()
})

test('login links to forgot-password', async ({ page }) => {
  await page.goto('/login')
  const link = page.getByRole('link', { name: 'Forgot password?' })
  await expect(link).toHaveAttribute('href', '/forgot-password')
})

test('forgot-password shows a check-your-email confirmation', async ({ page }) => {
  await page.goto('/forgot-password')
  await page.getByLabel('Email').fill('nobody@example.com')
  await Promise.all([
    page.waitForResponse((r) => r.url().includes('/password-reset/request')),
    page.getByRole('button', { name: 'Reset password' }).click(),
  ])
  await expect(page.getByRole('heading', { name: /Check your email/i })).toBeVisible()
})

test('verify-email with a bad token shows failure', async ({ page }) => {
  await page.goto('/verify-email?token=not-a-real-token')
  await expect(page.getByRole('heading', { name: /Verification failed/i })).toBeVisible()
})

test('theme toggle switches and persists across reload', async ({ page }) => {
  await page.goto('/login')
  const html = page.locator('html')

  // Default is dark (no .light class).
  await expect(html).not.toHaveClass(/light/)
  await page.getByRole('button', { name: /Switch to light mode/i }).click()
  await expect(html).toHaveClass(/light/)

  await page.reload()
  await expect(html).toHaveClass(/light/) // persisted via localStorage

  // Flip back so the choice doesn't leak into other specs' storage state.
  await page.getByRole('button', { name: /Switch to dark mode/i }).click()
  await expect(html).not.toHaveClass(/light/)
})
