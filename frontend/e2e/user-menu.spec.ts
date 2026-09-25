import { test, expect } from '@playwright/test'
import { DEV_USER, login, openProblemWithCode, SOLVE_PROBLEM, SOLVE_WRONG } from './helpers'

// Judging runs a real container, so give the verdict room.
const VERDICT_TIMEOUT = 45_000

test('the account menu stays on top of the workspace after a submit', async ({ page }) => {
  await login(page, DEV_USER.email, DEV_USER.password)
  await expect(page).toHaveURL(/\/problems$/)   // signed in before navigating on
  await openProblemWithCode(page, SOLVE_PROBLEM.slug, SOLVE_WRONG)
  await page.getByRole('button', { name: 'Submit' }).click()
  await expect(page.getByText('Wrong Answer').first()).toBeVisible({ timeout: VERDICT_TIMEOUT })

  // Playwright refuses to click an element another one covers, so this fails if any
  // workspace pane paints over the dropdown.
  await page.getByRole('button', { name: /Account menu/ }).click()
  await page.getByRole('button', { name: 'Sign out' }).click({ timeout: 5_000 })
  await expect(page).toHaveURL(/\/login$/)
})
