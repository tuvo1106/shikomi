import { test, expect } from '@playwright/test'
import { login, registerNewUser, totpCode, verifyEmailViaCli } from './helpers'

test('enrol in two-factor auth, then sign in with a code and with a recovery code', async ({ page }) => {
  const creds = await registerNewUser(page)
  verifyEmailViaCli(creds.email)
  await login(page, creds.email, creds.password)
  await expect(page).toHaveURL(/\/problems/)

  // Enrol: read the manual key off the page, prove possession with a code.
  await page.goto('/settings')
  await page.getByRole('button', { name: 'Set up two-factor' }).click()
  await expect(page.getByRole('img', { name: /QR code/i })).toBeVisible()
  const secret = (await page.locator('code').first().innerText()).replace(/\s/g, '')
  await page.getByLabel('6-digit code').fill(totpCode(secret))
  await page.getByRole('button', { name: 'Verify and turn on' }).click()

  // Recovery codes are shown once; grab one to use later.
  await expect(page.getByText(/can't show them again/i)).toBeVisible()
  const recovery = (await page.locator('li').filter({ hasText: /^[a-z0-9]{4}(-[a-z0-9]{4}){3}$/i }).first().innerText()).trim()
  await page.getByLabel(/saved these codes/i).check()
  await page.getByRole('button', { name: 'Done' }).click()
  await expect(page.getByText('Signing in asks for a code')).toBeVisible()

  // Fresh browser state → password alone must no longer sign in.
  await page.context().clearCookies()
  await page.evaluate(() => localStorage.clear())
  await login(page, creds.email, creds.password)
  await expect(page.getByRole('heading', { name: 'Two-factor authentication' })).toBeVisible()
  await expect(page).not.toHaveURL(/\/problems/)

  // The enrolment code is spent (single use), so the next step's code is the valid one.
  await page.getByLabel('Authentication code').fill(totpCode(secret, 1))
  await page.getByRole('button', { name: 'Two-factor authentication' }).click()
  await expect(page).toHaveURL(/\/problems/)

  // And a recovery code works in place of the app.
  await page.context().clearCookies()
  await page.evaluate(() => localStorage.clear())
  await login(page, creds.email, creds.password)
  await page.getByLabel('Authentication code').fill(recovery)
  await page.getByRole('button', { name: 'Two-factor authentication' }).click()
  await expect(page).toHaveURL(/\/problems/)
})
