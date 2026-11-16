import { cn } from '@/lib/cn'
import { useId, useState, type ReactNode } from 'react'
import { Icon } from './Icon'

export type Tone = 'green' | 'amber' | 'red' | 'blue' | 'purple' | 'gray'

const TONES: Record<Tone, string> = {
  green: 'bg-emerald-50 text-emerald-800',
  amber: 'bg-orange-50 text-orange-800',
  red: 'bg-red-50 text-red-800',
  blue: 'bg-brand-soft text-brand',
  purple: 'bg-violet-50 text-violet-800',
  gray: 'bg-slate-100 text-slate-700',
}

export function Badge({ tone = 'gray', children, className, title }: { tone?: Tone; children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cn('inline-flex max-w-full items-center px-2 py-1 text-xs leading-4 font-semibold break-words', TONES[tone], className)}>
      {children}
    </span>
  )
}

const STATUS_TONES: Record<Tone, [string, string]> = {
  green: ['bg-emerald-600', 'text-emerald-700'],
  amber: ['bg-amber-500', 'text-orange-700'],
  red: ['bg-red-600', 'text-red-700'],
  blue: ['bg-brand', 'text-brand'],
  purple: ['bg-violet-600', 'text-violet-700'],
  gray: ['bg-slate-400', 'text-slate-600'],
}

/** Portal status marker: coloured dot and spaced capitals, as on offer and survey cards. */
export function Status({ tone, children }: { tone: Tone; children: ReactNode }) {
  const [dot, text] = STATUS_TONES[tone]
  return (
    <span className={cn('inline-flex items-center gap-2 text-xs font-bold tracking-wider uppercase', text)}>
      <span aria-hidden="true" className={cn('h-2 w-2 shrink-0 rounded-full', dot)} />
      {children}
    </span>
  )
}

// Portal cards: white, borderless, on the Pale Blue page.
export const card = 'min-w-0 bg-white'

export function Section({ title, children, className, right }: { title: ReactNode; children: ReactNode; className?: string; right?: ReactNode }) {
  return (
    <section className={cn(card, 'p-4 sm:p-5', className)}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-ink text-lg font-bold">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  )
}

export function Collapsible({ title, children, defaultOpen = false, open: controlledOpen, onOpenChange }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const [localOpen, setOpen] = useState(defaultOpen)
  const open = controlledOpen ?? localOpen
  const id = useId()
  return (
    <section className={card}>
      <button
        type="button"
        onClick={() => {
          setOpen(!open)
          onOpenChange?.(!open)
        }}
        className="text-ink hover:bg-brand-soft/50 flex min-h-12 w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm font-bold sm:px-5"
        aria-expanded={open}
        aria-controls={id}
      >
        <span>{title}</span>
        <Icon name="chevron" className={cn('text-brand shrink-0 transition-transform', open && 'rotate-180')} />
      </button>
      <div id={id} hidden={!open} className="px-4 pb-4 sm:px-5">
        {open && children}
      </div>
    </section>
  )
}
