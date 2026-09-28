import { test, expect, type Locator } from '@playwright/test'
import { DEV_USER, login } from './helpers'

/**
 * The navbar's links share a baseline with the brand only through a 1px nudge
 * (`sm:pt-0.5` in Navbar.tsx) that depends on the fonts' metrics and sizes. This
 * measures both baselines in the browser, so a font, size or bar-height change
 * that breaks the alignment fails here instead of going unnoticed.
 *
 * The app uses the system font stack, so the true gap is a sub-pixel amount that
 * rounds differently per OS: 0px on macOS, 1px on CI's Linux. A 1px tolerance is
 * therefore the tightest honest bound. It still catches the bug the nudge fixed,
 * which measured 2px (a bottom-only border and no nudge).
 */
function baseline(el: Locator): Promise<number> {
  return el.evaluate((node) => {
    // A zero-height inline-block sits on its line's baseline, so its top *is* the
    // baseline. It goes inside a span wrapped around the text, so that in a flex
    // container (the nav tab) it joins the text's line instead of becoming a flex
    // item of its own; the wrapper is the flex item the bare text already was.
    const text = [...node.childNodes].find((n) => n.nodeType === Node.TEXT_NODE && n.textContent?.trim())
    if (!text) throw new Error('no text to measure')
    const wrap = document.createElement('span')
    text.replaceWith(wrap)
    wrap.appendChild(text)
    const probe = document.createElement('span')
    probe.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline'
    wrap.appendChild(probe)
    const y = probe.getBoundingClientRect().top
    wrap.replaceWith(text)
    return y
  })
}

test("the nav links share the brand text's baseline", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await login(page, DEV_USER.email, DEV_USER.password)
  await expect(page).toHaveURL(/\/problems$/)
  const brand = await baseline(page.getByTestId('brand-text'))
  // Every entry of NAV_LINKS (navLinks.ts); today that's one.
  for (const label of ['Problems']) {
    const link = page.getByRole('navigation').getByRole('link', { name: label, exact: true })
    expect(Math.abs((await baseline(link)) - brand)).toBeLessThanOrEqual(1)
  }
})
