import { cn } from '@/lib/cn'

const PATHS = {
  search: 'm21 21-4.4-4.4 M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  check: 'm5 12 4 4L19 6',
  chevron: 'm6 9 6 6 6-6',
  arrow: 'M4 12h16 m-6-6 6 6-6 6',
  copy: 'M9 9h12v12H9z M15 5V3H3v12h2',
  document: 'M14 2H5v20h14V7z M14 2v5h5 M8 12h8 M8 16h8',
  edit: 'm16 3 5 5-12 12-6 1 1-6z M14 5l5 5',
  undo: 'm9 4-5 5 5 5 M4 9h10a6 6 0 0 1 0 12',
  plus: 'M12 5v14 M5 12h14',
  info: 'M12 11v6 M12 7h.01 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  alert: 'm12 3 10 18H2z M12 9v5 M12 17h.01',
  download: 'M12 4v11 m-5-5 5 5 5-5 M4 20h16',
  upload: 'M12 16V5 m-5 5 5-5 5 5 M4 20h16',
  refresh: 'M20 7v5h-5 M4 17v-5h5 M6 6a8 8 0 0 1 14 6 M18 18a8 8 0 0 1-14-6',
} as const

export function Icon({ name, className }: { name: keyof typeof PATHS; className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" className={cn('h-4 w-4', className)}>
      <path d={PATHS[name]} />
    </svg>
  )
}
