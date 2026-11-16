import { ExamplesPanel } from '@/components/ktru/ExamplesPanel'
import { PageHeader } from '@/components/layout/AppShell'
import { useNavigate } from '@tanstack/react-router'

export function ExamplesPage() {
  const navigate = useNavigate()
  return (
    <>
      <PageHeader crumbs={['Каталог продукции', 'Примеры СТЕ']} title="Товары с Портала поставщиков">
        <p className="mt-3 max-w-2xl text-base leading-7 text-slate-600">Реальные описания стандартных товарных единиц. Выберите товар, чтобы подобрать для него код КТРУ и заполнить карточку.</p>
      </PageHeader>
      <ExamplesPanel disabled={false} count={12} title="Каталог примеров" onPick={(text) => void navigate({ to: '/', search: { text } })} className="mx-auto mt-10 max-w-[1440px] px-4 sm:px-6" />
    </>
  )
}
