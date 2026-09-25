import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Modal } from './Modal'

function setup(onClose = vi.fn()) {
  render(
    <>
      <button>opener</button>
      <Modal label="Demo" onClose={onClose} className="max-w-sm">
        <button>inside</button>
      </Modal>
    </>,
  )
  return onClose
}

describe('Modal', () => {
  it('is an accessible modal dialog with focus moved into it', () => {
    setup()
    const dialog = screen.getByRole('dialog', { name: 'Demo' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog).toHaveFocus()
  })

  it('closes on Escape and on a scrim click, but not on a click inside the panel', async () => {
    const onClose = setup()
    await userEvent.click(screen.getByText('inside'))
    expect(onClose).not.toHaveBeenCalled()
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
    await userEvent.click(screen.getByRole('dialog').parentElement!)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('caps its height to the visible viewport and scrolls inside, so a short screen never clips it', () => {
    setup()
    // jsdom does no layout; these classes ARE the fix (see e2e/responsive.spec.ts for
    // the real-browser check that the panel fits a landscape phone).
    const cls = screen.getByRole('dialog').className
    expect(cls).toContain('max-h-[calc(100dvh-2rem)]')
    expect(cls).toContain('overflow-y-auto')
  })

  it('keeps focus in the panel across parent re-renders that pass a new onClose', () => {
    const { rerender } = render(<Modal label="x" onClose={() => {}}>hi</Modal>)
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveFocus()
    rerender(<Modal label="x" onClose={() => {}}>hi</Modal>)
    expect(dialog).toHaveFocus()
  })

  it('hands focus back to what opened it', () => {
    const { unmount } = render(<button>opener</button>)
    screen.getByText('opener').focus()
    unmount()
    const opener = document.createElement('button')
    document.body.appendChild(opener)
    opener.focus()
    const { unmount: close } = render(<Modal label="x" onClose={() => {}}>hi</Modal>)
    expect(document.activeElement).not.toBe(opener)
    close()
    expect(document.activeElement).toBe(opener)
    opener.remove()
  })
})
