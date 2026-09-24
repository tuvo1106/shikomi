import { test, expect } from '@playwright/test'
import {
  DEV_USER,
  login,
  openProblemWithCode,
  SOLVE_CORRECT,
  SOLVE_PROBLEM,
  SOLVE_WRONG,
} from './helpers'

// Exercises the real judge, so give verdicts room (Docker container spin-up).
const VERDICT_TIMEOUT = 45_000

test('submit wrong then correct solution, verdict + history', async ({ page }) => {
  // Judging is gated behind a verified email; the seeded dev user is verified.
  await login(page, DEV_USER.email, DEV_USER.password)
  // Search rather than assume the problem is on the first page: an operator's own
  // catalog may be loaded alongside the starters.
  await page.getByPlaceholder('Search problems…').fill(SOLVE_PROBLEM.title)
  await expect(page.getByRole('link', { name: SOLVE_PROBLEM.title, exact: true })).toBeVisible()

  // A wrong solution is rejected.
  await openProblemWithCode(page, SOLVE_PROBLEM.slug, SOLVE_WRONG)
  await page.getByRole('button', { name: 'Submit' }).click()
  await expect(page.getByText('Wrong Answer').first()).toBeVisible({ timeout: VERDICT_TIMEOUT })

  // A correct solution is accepted and celebrated.
  await openProblemWithCode(page, SOLVE_PROBLEM.slug, SOLVE_CORRECT)
  await page.getByRole('button', { name: 'Submit' }).click()
  await expect(page.getByRole('heading', { name: 'Accepted' })).toBeVisible({
    timeout: VERDICT_TIMEOUT,
  })

  // The success modal links to the submissions history, which lists both tries.
  // (The seeded dev user may have prior rows, so scope to the first of each.)
  await page.getByRole('button', { name: 'View submissions' }).click()
  await expect(page.getByText('Accepted').first()).toBeVisible()
  await expect(page.getByText('Wrong Answer').first()).toBeVisible()
})
