import type { Language } from '../../api/types'
import { LANGUAGE_LABEL } from '../../lib/languages'

/**
 * The editor header's language picker, shown only when a problem offers more
 * than one language (a one-language problem keeps its plain label).
 *
 * A segmented control rather than a `<select>`: with a handful of languages
 * every option stays visible, so you can see at a glance which languages you've
 * started (the dot marks a draft that differs from its starter code) without
 * opening anything. It is disabled while a verdict is pending, because the
 * result being polled belongs to the language it was submitted in.
 */
export function LanguageSwitcher({
  languages,
  value,
  edited,
  disabled,
  onChange,
}: {
  languages: Language[]
  value: Language
  edited: Set<Language>
  disabled: boolean
  onChange: (language: Language) => void
}) {
  return (
    <div
      role="group"
      aria-label="Language"
      className="inline-flex rounded-md border border-zinc-800 bg-zinc-900/60 p-0.5"
    >
      {languages.map((lang) => {
        const active = lang === value
        return (
          <button
            key={lang}
            type="button"
            aria-pressed={active}
            disabled={disabled}
            onClick={() => onChange(lang)}
            title={edited.has(lang) ? `${LANGUAGE_LABEL[lang]} (edited)` : LANGUAGE_LABEL[lang]}
            className={`flex items-center gap-1.5 rounded px-2.5 py-0.5 disabled:cursor-not-allowed disabled:opacity-50 ${
              active
                ? 'bg-zinc-800 text-zinc-100'
                : 'text-zinc-500 hover:text-zinc-300'
            }`}
          >
            {LANGUAGE_LABEL[lang]}
            {edited.has(lang) && (
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-indigo-400" />
            )}
          </button>
        )
      })}
    </div>
  )
}
