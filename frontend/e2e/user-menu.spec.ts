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

test('no z-index inside Monaco can reach past the navbar', async ({ page }) => {
  // The test above is the user-facing regression, but it can't reach Monaco's own
  // widgets: pressing the avatar blurs the editor, which cancels the suggest widget
  // before the dropdown opens. Those widgets are the reason the editor pane carries
  // `isolate` — `.monaco-editor` is `position: relative; z-index: auto`, so without a
  // stacking context its z-indexes (suggest 40, rename box 100, overlay message
  // 10000) compete with the bar's `z-40` in the root one. Stand in for them with an
  // injected element, since nothing in a normal session opens a widget over the
  // dropdown and stays open.
  await login(page, DEV_USER.email, DEV_USER.password)
  await expect(page).toHaveURL(/\/problems$/)
  await openProblemWithCode(page, SOLVE_PROBLEM.slug, SOLVE_WRONG)
  await page.waitForSelector('.monaco-editor', { state: 'attached' })

  // Open the menu first, then inject: the stand-in covers the viewport, so injecting
  // it earlier would swallow the click on the avatar and time out instead of failing
  // the assertion below.
  await page.getByRole('button', { name: /Account menu/ }).click()
  const signOut = (await page.getByRole('button', { name: 'Sign out' }).boundingBox())!
  await page.evaluate(() => {
    const widget = document.createElement('div')
    widget.id = 'fake-monaco-widget'
    widget.style.cssText = 'position:fixed;inset:0;z-index:10000'
    document.querySelector('.monaco-editor')!.appendChild(widget)
  })

  // Hit-test rather than click: a click would assert the same thing, but this says
  // which element won when it fails.
  const topmost = await page.evaluate(
    ([x, y]) => (document.elementFromPoint(x, y) as HTMLElement).id || 'dropdown',
    [signOut.x + signOut.width / 2, signOut.y + signOut.height / 2] as const,
  )
  expect(topmost).toBe('dropdown')
})
