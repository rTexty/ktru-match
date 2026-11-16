// Buttons follow the portal kit: red filled primary, Gray Blue outline secondary, text link small.
export const btn = {
  primary: 'inline-flex min-h-12 items-center justify-center gap-2 bg-action px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-action-dark disabled:cursor-not-allowed disabled:bg-brand-soft disabled:text-slate-500',
  secondary:
    'inline-flex min-h-12 items-center justify-center gap-2 border border-line bg-white px-5 py-2 text-sm font-semibold text-brand transition-colors hover:border-brand disabled:cursor-not-allowed disabled:text-brand/50 disabled:hover:border-line',
  small: 'inline-flex min-h-11 items-center justify-center gap-1.5 px-2 py-2 text-xs font-semibold text-brand underline-offset-4 transition-colors hover:underline disabled:cursor-not-allowed disabled:text-slate-400 disabled:no-underline',
}

// Filled Pale Blue field; the border turns Main Blue on focus.
export const field = 'border border-transparent bg-brand-soft text-sm text-ink placeholder:text-slate-500 transition-colors hover:border-line focus:border-brand focus:outline-none focus-visible:outline-none'

export function scrollBehavior(): ScrollBehavior {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'
}
