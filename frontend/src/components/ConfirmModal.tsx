import type { ReactNode } from 'react'
import { Modal } from './Modal'

/** A small centered confirm dialog: dark scrim, Cancel + a danger action. */
export function ConfirmModal({
  message,
  confirmLabel = 'Delete',
  onConfirm,
  onCancel,
}: {
  message: ReactNode
  confirmLabel?: string
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Modal label="Confirm" onClose={onCancel} className="max-w-sm p-5">
      <div className="text-sm text-zinc-300">{message}</div>
      <div className="mt-4 flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="rounded border border-zinc-700 px-3 py-1.5 text-sm text-zinc-200 hover:bg-zinc-800"
        >
          Cancel
        </button>
        <button
          onClick={onConfirm}
          className="rounded bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-500"
        >
          {confirmLabel}
        </button>
      </div>
    </Modal>
  )
}
