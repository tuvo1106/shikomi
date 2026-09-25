import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import type { User } from '../../api/types'
import { UserMenu } from './UserMenu'

const USER: User = {
  id: 'u1', email: 'a@example.com', username: 'alice', email_verified: true, totp_enabled: false,
}

function setup(user: User, at = '/') {
  render(
    <MemoryRouter initialEntries={[at]}>
      <UserMenu user={user} logout={() => {}} />
    </MemoryRouter>,
  )
  return screen.getByRole('button', { name: /Account menu/ })
}

describe('UserMenu trigger', () => {
  it('has an accessible name even when the username text is hidden on phones', () => {
    expect(setup(USER)).toHaveAccessibleName('Account menu, alice')
  })

  it('reports whether the menu is open', async () => {
    const button = setup(USER)
    expect(button).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('UserMenu navigation links', () => {
  // The links only show below 360px (CSS, which jsdom doesn't apply), so this pins that
  // they're in the menu and hide again at >=360px via the class.
  it('offers the nav links, hidden from 360px up where the navbar shows them itself', async () => {
    await userEvent.click(setup(USER))
    const group = screen.getByTestId('menu-nav-links')
    expect(group).toHaveClass('min-[360px]:hidden')
    expect(group.querySelectorAll('a')).toHaveLength(1)
    expect(screen.getByRole('link', { name: 'Problems' })).toHaveAttribute('href', '/problems')
  })
})

describe('UserMenu active link', () => {
  it('marks the current page, like the navbar links do', async () => {
    await userEvent.click(setup(USER, '/problems'))
    expect(screen.getByRole('link', { name: 'Problems' })).toHaveAttribute('aria-current', 'page')
  })

  it('leaves the link unmarked elsewhere', async () => {
    await userEvent.click(setup(USER, '/settings'))
    expect(screen.getByRole('link', { name: 'Problems' })).not.toHaveAttribute('aria-current')
  })
})
