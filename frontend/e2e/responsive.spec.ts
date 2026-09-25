import { test, expect } from '@playwright/test'
import { DEV_USER, login, openProblemWithCode, SOLVE_CORRECT, SOLVE_PROBLEM } from './helpers'

test.describe('landscape phone', () => {
  // A phone held sideways is only ~375px tall: a centered modal without a height cap
  // overflows both ends and pushes the Close button off-screen. jsdom does no layout,
  // so this is the check that actually catches it.
  test.use({ viewport: { width: 667, height: 375 } })

  test('accepted modal fits and stays reachable', async ({ page }) => {
    await login(page, DEV_USER.email, DEV_USER.password)
    await expect(page).toHaveURL(/\/problems/) // `login` doesn't wait; navigating early cancels it
    await openProblemWithCode(page, SOLVE_PROBLEM.slug, SOLVE_CORRECT)
    await page.getByRole('button', { name: 'Submit' }).click()

    const dialog = page.getByRole('dialog', { name: 'Accepted' })
    await expect(dialog).toBeVisible({ timeout: 45_000 })

    const box = (await dialog.boundingBox())!
    expect(box.y).toBeGreaterThanOrEqual(0)
    expect(box.y + box.height).toBeLessThanOrEqual(375)

    // Content taller than the panel scrolls inside it, so the last button is reachable.
    await page.getByRole('button', { name: 'Close' }).click()
    await expect(dialog).toBeHidden()
  })
})

test.describe('narrow phone', () => {
  // The signed-in navbar (brand + nav links + theme toggle + avatar) must never make the
  // page scroll sideways, down to the narrowest phones.
  for (const width of [280, 320, 340, 360]) {
    test(`navbar fits ${width}px without horizontal page scroll`, async ({ page }) => {
      await page.setViewportSize({ width, height: 640 })
      await login(page, DEV_USER.email, DEV_USER.password)
      await expect(page).toHaveURL(/\/problems/)
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      )
      expect(overflow).toBeLessThanOrEqual(0)
    })
  }

  // Below 360px the links leave the bar for the user menu, so nothing is unreachable.
  test('below 360px the nav links live in the user menu', async ({ page }) => {
    await page.setViewportSize({ width: 340, height: 640 })
    await login(page, DEV_USER.email, DEV_USER.password)
    await expect(page).toHaveURL(/\/problems/)
    await page.goto('/settings')
    await expect(page.getByRole('navigation').getByRole('link', { name: 'Problems' })).toBeHidden()
    await page.getByRole('button', { name: /Account menu/ }).click()
    await page.getByRole('link', { name: 'Problems' }).click()
    await expect(page).toHaveURL(/\/problems$/)
  })

  test('from 360px the links are in the bar and not duplicated in the menu', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 640 })
    await login(page, DEV_USER.email, DEV_USER.password)
    await expect(page).toHaveURL(/\/problems/)
    await expect(page.getByRole('navigation').getByRole('link', { name: 'Problems' })).toBeVisible()
    await page.getByRole('button', { name: /Account menu/ }).click()
    await expect(page.getByTestId('menu-nav-links')).toBeHidden()
  })
})
