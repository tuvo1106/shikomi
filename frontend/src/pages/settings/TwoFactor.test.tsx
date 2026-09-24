import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TwoFactor } from './TwoFactor'
import { json } from '../../test/http'

const SETUP = { secret: 'JBSWY3DPEHPK3PXP', otpauth_uri: 'otpauth://totp/shikomi:a@b.com?secret=JBSWY3DPEHPK3PXP' }
const CODES = ['aaaa-bbbb-cccc-dddd', 'eeee-ffff-gggg-hhhh']

/** Stub fetch with per-path responses; returns the mock so tests can inspect calls. */
function stub(routes: Record<string, () => Response>) {
  const fn = vi.fn((input: string) => {
    const hit = Object.entries(routes).find(([path]) => String(input).includes(path))
    return Promise.resolve(hit ? hit[1]() : json({}, 404))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

const bodyOf = (fn: ReturnType<typeof stub>, path: string) => {
  const call = fn.mock.calls.find(([u]) => String(u).includes(path)) as unknown as [string, RequestInit]
  return JSON.parse(String(call[1].body))
}

afterEach(() => vi.unstubAllGlobals())

describe('TwoFactor', () => {
  it('enrols: QR + key, verify a code, then holds the recovery codes until acknowledged', async () => {
    const onChanged = vi.fn()
    stub({
      '/auth/2fa/setup': () => json(SETUP),
      '/auth/2fa/enable': () => json({ recovery_codes: CODES }),
    })
    const user = userEvent.setup()
    render(<TwoFactor enabled={false} onChanged={onChanged} />)

    await user.click(screen.getByRole('button', { name: 'Set up two-factor' }))
    expect(await screen.findByRole('img', { name: /QR code/i })).toBeInTheDocument()
    expect(screen.getByText('JBSW Y3DP EHPK 3PXP')).toBeInTheDocument()

    await user.type(screen.getByLabelText('6-digit code'), '123456')
    await user.click(screen.getByRole('button', { name: 'Verify and turn on' }))

    // Codes shown; the parent is NOT told yet (refreshing would unmount the codes).
    expect(await screen.findByText(CODES[0])).toBeInTheDocument()
    expect(onChanged).not.toHaveBeenCalled()

    const done = screen.getByRole('button', { name: 'Done' })
    expect(done).toBeDisabled()
    await user.click(screen.getByLabelText(/saved these codes/i))
    await user.click(done)
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
  })

  it('shows a wrong code inline and stays on the verify step', async () => {
    stub({
      '/auth/2fa/setup': () => json(SETUP),
      '/auth/2fa/enable': () => json({ detail: 'That code is not valid.', code: 'INVALID_CODE' }, 400),
    })
    const user = userEvent.setup()
    render(<TwoFactor enabled={false} onChanged={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Set up two-factor' }))
    await user.type(await screen.findByLabelText('6-digit code'), '000000')
    await user.click(screen.getByRole('button', { name: 'Verify and turn on' }))
    expect(await screen.findByText('That code is not valid.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Done' })).not.toBeInTheDocument()
  })

  it('turns off with password + code, then refreshes the user', async () => {
    const onChanged = vi.fn()
    const fetchMock = stub({ '/auth/2fa/disable': () => json({}) })
    const user = userEvent.setup()
    render(<TwoFactor enabled onChanged={onChanged} />)

    await user.click(screen.getByRole('button', { name: 'Turn off' }))
    await user.type(screen.getByLabelText('Password'), 'pw-secret')
    await user.type(screen.getByLabelText('Authentication code'), ' 654321 ')
    await user.click(screen.getByRole('button', { name: 'Turn off' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
    expect(bodyOf(fetchMock, '/auth/2fa/disable')).toEqual({ password: 'pw-secret', code: '654321' })
  })

  it('regenerates recovery codes with a current code', async () => {
    const fetchMock = stub({ '/auth/2fa/recovery-codes': () => json({ recovery_codes: CODES }) })
    const user = userEvent.setup()
    render(<TwoFactor enabled onChanged={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'New recovery codes' }))
    await user.type(screen.getByLabelText('Authentication code'), '111222')
    await user.click(screen.getByRole('button', { name: 'Generate new codes' }))

    expect(await screen.findByText(CODES[1])).toBeInTheDocument()
    expect(bodyOf(fetchMock, '/auth/2fa/recovery-codes')).toEqual({ code: '111222' })
  })
})
