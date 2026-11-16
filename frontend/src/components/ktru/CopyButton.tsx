import { useState } from 'react'
import { Icon } from './Icon'
import { btn } from './styles'

export function CopyButton({ value, label = 'Копировать', disabled = false }: { value: string; label?: string; disabled?: boolean }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [copiedValue, setCopiedValue] = useState('')
  return (
    <div className="min-w-0">
      <button
        type="button"
        className={btn.small}
        disabled={disabled}
        onClick={async () => {
          try {
            if (!navigator.clipboard) throw new Error('Clipboard unavailable')
            await navigator.clipboard.writeText(value)
            setCopiedValue(value)
            setState('copied')
          } catch {
            setState('failed')
          }
        }}
      >
        <Icon name={state === 'copied' && copiedValue === value ? 'check' : 'copy'} />
        {state === 'copied' && copiedValue === value ? 'Скопировано' : label}
      </button>
      {state === 'failed' && (
        <p role="alert" className="max-w-xs text-xs leading-5 text-red-800">
          {label === 'Копировать' ? 'Не удалось скопировать. Выделите код и скопируйте вручную.' : 'Не удалось скопировать. Выделите значение и скопируйте вручную.'}
        </p>
      )}
    </div>
  )
}
