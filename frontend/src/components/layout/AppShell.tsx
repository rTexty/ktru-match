import { Badge } from '@/components/ktru/ui'
import { cn } from '@/lib/cn'
import { Link, Outlet } from '@tanstack/react-router'
import type { ReactNode } from 'react'
import { useHealth } from './useHealth'

const NAV = [
  { to: '/', label: 'Подбор КТРУ' },
  { to: '/batch', label: 'Загрузка CSV' },
  { to: '/examples', label: 'Примеры СТЕ' },
  { to: '/about', label: 'Как это работает' },
] as const

export function HealthBadge() {
  const { data, isError } = useHealth()
  if (isError) return <Badge tone="red">API недоступно</Badge>
  if (!data) return <Badge>…</Badge>
  const active = data.counts.positions_active ?? data.counts.active_positions
  return (
    <Badge tone={data.ready ? 'green' : 'amber'}>
      {data.ready ? 'КТРУ' : 'Не готово'}
      {data.snapshot_date ? ` от ${data.snapshot_date}` : ''}
      {active !== undefined ? ` · ${active.toLocaleString('ru-RU')} позиций` : ''}
    </Badge>
  )
}

function Logo({ className }: { className?: string }) {
  return (
    <Link to="/" className={cn('text-brand block shrink-0 text-lg font-bold whitespace-nowrap', className)}>
      <span>KTRU <span className="text-ink">Match</span></span>
    </Link>
  )
}

export function AppShell() {
  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" className="text-brand sr-only z-50 bg-white p-3 focus:not-sr-only focus:fixed focus:top-2 focus:left-2">
        Перейти к содержимому
      </a>
      <header className="z-40 bg-white shadow-[0_4px_16px_rgba(38,75,130,0.08)] lg:sticky lg:top-0">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center justify-between gap-x-10 px-4 sm:px-6 lg:h-20 lg:flex-nowrap">
          <Logo className="flex h-16 items-center lg:h-auto" />
          <div className="lg:order-3">
            <HealthBadge />
          </div>
          <nav aria-label="Разделы" className="-mx-4 flex w-full min-w-0 overflow-x-auto px-4 sm:mx-0 sm:px-0 lg:order-2 lg:w-auto lg:flex-1 lg:self-stretch">
            {NAV.map((item) => (
              <Link
                key={item.to}
                to={item.to}
                activeOptions={{ exact: true }}
                className="hover:text-brand flex min-h-12 shrink-0 items-center border-b-[3px] border-transparent px-3 text-sm font-semibold whitespace-nowrap text-slate-600 first:pl-0 lg:px-4"
                activeProps={{ className: 'border-brand! text-brand!', 'aria-current': 'page' }}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </div>
      </header>
      <main id="main" className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  )
}

function Footer() {
  return (
    <footer className="border-line mt-16 border-t bg-white">
      <div className="mx-auto grid max-w-[1440px] gap-8 px-4 py-10 sm:px-6 md:grid-cols-[auto_1fr_auto] md:items-start">
        <Logo />
        <nav aria-label="Разделы в подвале" className="flex flex-wrap gap-x-8 gap-y-3 text-sm">
          {NAV.map((item) => (
            <Link key={item.to} to={item.to} className="text-ink hover:text-brand underline-offset-4 hover:underline">
              {item.label}
            </Link>
          ))}
        </nav>
        <p className="max-w-sm text-xs leading-5 text-slate-600">Характеристики и коды берутся из актуального КТРУ. Описание обрабатывается открытыми моделями на сервере сервиса.</p>
      </div>
    </footer>
  )
}

/** Page band: breadcrumbs, the page title and an optional aside (steps, actions). */
export function PageHeader({ crumbs, title, aside, children }: { crumbs: string[]; title: ReactNode; aside?: ReactNode; children?: ReactNode }) {
  return (
    <div className="mx-auto max-w-[1440px] px-4 pt-6 sm:px-6 sm:pt-8">
      <nav aria-label="Навигационная цепочка" className="mb-2 text-xs text-slate-600">
        <ol className="flex flex-wrap gap-1.5">
          {crumbs.map((crumb, index) => (
            <li key={crumb} className="flex gap-1.5">
              {index > 0 && <span aria-hidden="true">/</span>}
              {crumb}
            </li>
          ))}
        </ol>
      </nav>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <h1 className="text-ink max-w-5xl text-[28px] leading-9 font-bold sm:text-[40px] sm:leading-[52px]">{title}</h1>
        {aside}
      </div>
      {children}
    </div>
  )
}
