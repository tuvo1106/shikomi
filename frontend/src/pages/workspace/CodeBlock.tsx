import { useState } from 'react'
import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter'
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python'
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript'
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql'
import { oneDark, oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { useTheme } from '../../lib/theme'
import type { Language } from '../../api/types'

SyntaxHighlighter.registerLanguage('python', python)
SyntaxHighlighter.registerLanguage('javascript', javascript)
SyntaxHighlighter.registerLanguage('sql', sql)

const PRISM_LANGUAGE: Record<Language, string> = { python: 'python', js: 'javascript', mysql: 'sql' }

/**
 * Read-only syntax-highlighted code block (theme-aware), with an optional copy
 * button. Uses `PrismLight` + only the registered grammars this app actually
 * needs, not Prism's full language set. `language` picks the grammar — the
 * problem's own `language` field (DESIGN.md §13), defaulting to `python` for
 * callers that don't have it in scope yet. `copyable` is off where a "Load
 * into editor" action already covers copying (the submission detail modal).
 */
export function CodeBlock({
  code,
  language = 'python',
  copyable = true,
}: {
  code: string
  language?: Language
  copyable?: boolean
}) {
  const [copied, setCopied] = useState(false)
  const { theme } = useTheme()
  return (
    <div className="relative">
      {copyable && (
        <button
          onClick={() => {
            navigator.clipboard?.writeText(code)
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          }}
          className="absolute right-2 top-2 z-10 rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 text-xs text-zinc-400 hover:text-zinc-100"
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      )}
      <SyntaxHighlighter
        language={PRISM_LANGUAGE[language]}
        style={theme === 'dark' ? oneDark : oneLight}
        customStyle={{
          margin: 0,
          borderRadius: '0.375rem',
          fontSize: '12px',
          background: theme === 'dark' ? 'rgba(24, 24, 27, 0.6)' : 'rgba(244, 244, 245, 0.6)',
        }}
        codeTagProps={{ style: { fontFamily: 'ui-monospace, monospace' } }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  )
}
