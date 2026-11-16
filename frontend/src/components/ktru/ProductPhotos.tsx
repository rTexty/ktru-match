import { KtruApiError, ktruApi, type PortalCode, type PositionRef, type ProductPhoto } from '@/api/ktru'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { btn } from './styles'
import { Badge, card } from './ui'

const money = (amount: string, currency: string) => new Intl.NumberFormat('ru-RU', { style: 'currency', currency, maximumFractionDigits: 2 }).format(Number(amount))
// The Portal writes КПГЗ names in capitals («МОЛОТКИ»).
const tidy = (name: string | null | undefined) => (name && name === name.toUpperCase() ? name.charAt(0) + name.slice(1).toLowerCase() : (name ?? ''))
const SHOWN = 4
// One column in a narrow container, up to four tiles across the main column.
const GRID = 'grid gap-3 @md:grid-cols-2 @3xl:grid-cols-4'

function CodeBadges({ photo, ktru }: { photo: ProductPhoto; ktru: string }) {
  return (
    <p className="mt-1 flex flex-wrap gap-1">
      {photo.match === 'ktru' && (
        <Badge tone="green" title="Поставщик указал в карточке этот же код КТРУ">
          КТРУ {ktru}
        </Badge>
      )}
      {photo.okpd2 && (
        <Badge tone={photo.match === 'okpd2_related' ? 'amber' : 'blue'} title={`${photo.match === 'okpd2_related' ? 'Смежный код ОКПД2' : 'Тот же код ОКПД2, что у КТРУ'}${photo.okpd2.name ? `: ${tidy(photo.okpd2.name)}` : ''}`}>
          ОКПД2 {photo.okpd2.code}
          {photo.match === 'okpd2_related' && ' (смежный)'}
        </Badge>
      )}
      {photo.kpgz && (
        <Badge title={`КПГЗ Портала${photo.kpgz.name ? `: ${tidy(photo.kpgz.name)}` : ''}`}>
          КПГЗ {photo.kpgz.code}
          {photo.kpgz.name && ` · ${tidy(photo.kpgz.name)}`}
        </Badge>
      )}
    </p>
  )
}

/** Which ОКПД2 and КПГЗ the Portal products share, and how many products each covers. */
function MatchingCodes({ photos }: { photos: ProductPhoto[] }) {
  const count = (pick: (photo: ProductPhoto) => PortalCode | null) => {
    const seen = new Map<string, { code: PortalCode; n: number }>()
    for (const photo of photos) {
      const code = pick(photo)
      if (code) seen.set(code.code, { code, n: (seen.get(code.code)?.n ?? 0) + 1 })
    }
    return [...seen.values()].sort((a, b) => b.n - a.n)
  }
  const rows: [string, { code: PortalCode; n: number }[]][] = [
    ['ОКПД2', count((photo) => photo.okpd2)],
    ['КПГЗ', count((photo) => photo.kpgz)],
  ]
  const sameKtru = photos.filter((photo) => photo.match === 'ktru').length
  return (
    <dl className="bg-brand-soft mb-4 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 p-3 text-xs leading-5" data-testid="portal-codes">
      {sameKtru > 0 && (
        <>
          <dt className="font-semibold text-slate-600">КТРУ</dt>
          <dd className="text-ink">
            указан у {sameKtru} из {photos.length}
          </dd>
        </>
      )}
      {rows.map(([label, codes]) =>
        codes.length ? (
          <div key={label} className="contents">
            <dt className="font-semibold text-slate-600">{label}</dt>
            <dd className="text-ink min-w-0 break-words">
              {codes.map(({ code, n }) => (
                <span key={code.code} className="block">
                  {code.code}
                  {code.name && <span className="text-slate-600"> {tidy(code.name)}</span>}
                  <span className="text-slate-500">
                    {' '}
                    — {n} из {photos.length}
                  </span>
                </span>
              ))}
            </dd>
          </div>
        ) : null,
      )}
    </dl>
  )
}

function Photo({ photo, ktru }: { photo: ProductPhoto; ktru: string }) {
  const [failed, setFailed] = useState(false)
  const price = photo.price
  return (
    <li className="border-line flex min-w-0 flex-col gap-3 border bg-white p-3">
      {failed || !photo.image_url ? (
        <div aria-hidden="true" className="bg-brand-soft grid h-32 w-full place-items-center text-xs text-slate-500">
          нет фото
        </div>
      ) : (
        <img src={photo.image_url} alt="" className="h-32 w-full bg-white object-contain" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
      )}
      <div className="flex min-w-0 flex-1 flex-col text-xs leading-5">
        <a href={photo.source_url} target="_blank" rel="noopener noreferrer" className="text-brand line-clamp-2 text-sm leading-5 font-semibold underline-offset-2 hover:underline">
          {photo.name}
        </a>
        {price ? (
          <p className="text-ink mt-0.5 font-semibold" data-testid="portal-price">
            {money(price.amount, price.currency)}
            {price.unit && ` за ${price.unit}`}
            <span className="font-normal text-slate-500">{price.kind === 'median' ? ', медиана' : ', справочно'}</span>
          </p>
        ) : (
          <p className="mt-0.5 text-slate-500">Цена не указана</p>
        )}
        <p className="text-slate-500">
          СТЕ {photo.ste_id}, предложений: {photo.offers_count}
        </p>
        <div className="mt-auto pt-1">
          <CodeBadges photo={photo} ktru={ktru} />
        </div>
      </div>
    </li>
  )
}

/** Real Portal products under the chosen code: lets the supplier confirm the code by eye and see market prices. */
export function ProductPhotos({ position, text, waiting = false }: { position: PositionRef; text: string; waiting?: boolean }) {
  const photos = useQuery({
    queryKey: ['ktru', 'photos', position.snapshot_id, position.code, position.version, text],
    // Photos are optional context: an unexpected payload must not take the card down with it.
    queryFn: async () => {
      const data: unknown = await ktruApi.photos(position, text)
      if (!Array.isArray(data)) throw new Error('Не удалось получить товары с Портала.')
      return data as ProductPhoto[]
    },
    enabled: !waiting,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })
  const [expanded, setExpanded] = useState(false)
  // A KTRU code begins with its ОКПД2 code: 25.73.30.141-00000011 → 25.73.30.141.
  const okpd2 = position.code.split('-')[0]

  return (
    <section className={`${card} @container p-5`} data-testid="product-photos" aria-labelledby="portal-photos-title">
      <h2 id="portal-photos-title" className="text-ink text-base font-bold">
        Этот код на Портале поставщиков
      </h2>
      <p className="mt-1 mb-4 text-xs leading-5 text-slate-600">
        Товары Портала с кодом КТРУ {position.code} или с его ОКПД2 {okpd2}. Сравните со своим товаром.
      </p>
      {waiting || photos.isPending ? (
        <ul aria-busy="true" aria-label="Загружаем товары с Портала" className={GRID}>
          {[0, 1, 2, 3].map((i) => (
            <li key={i} className="border-line flex flex-col gap-3 border p-3 motion-safe:animate-pulse">
              <div className="bg-brand-soft h-32 w-full" />
              <div className="bg-brand-soft h-3 w-4/5" />
              <div className="bg-brand-soft h-3 w-1/2" />
            </li>
          ))}
        </ul>
      ) : photos.isError ? (
        <div className="text-sm leading-6 text-slate-600">
          <p>{photos.error instanceof KtruApiError ? photos.error.message : 'Не удалось получить товары с Портала.'}</p>
          <button type="button" className={btn.small} onClick={() => void photos.refetch()}>
            Повторить
          </button>
        </div>
      ) : photos.data.length === 0 ? (
        <p className="text-sm leading-6 text-slate-600">На Портале пока нет товаров с фото или ценой для этого кода.</p>
      ) : (
        <>
          <MatchingCodes photos={photos.data} />
          <ul className={GRID} data-testid="portal-products">
            {(expanded ? photos.data : photos.data.slice(0, SHOWN)).map((photo) => (
              <Photo key={`${photo.ste_id}:${photo.image_url}`} photo={photo} ktru={position.code} />
            ))}
          </ul>
          {photos.data.length > SHOWN && (
            <button type="button" className={`${btn.small} mt-3`} aria-expanded={expanded} onClick={() => setExpanded((open) => !open)}>
              {expanded ? 'Свернуть' : `Показать ещё ${photos.data.length - SHOWN}`}
            </button>
          )}
        </>
      )}
    </section>
  )
}
