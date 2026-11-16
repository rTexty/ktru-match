import type { Assertion, CardState, Correction, Fix } from '@/api/ktru'
import { cn } from '@/lib/cn'
import { Fragment, useState } from 'react'

const OP: Record<Assertion['operator'], string> = { eq: '', neq: '≠ ', gt: '> ', ge: '≥ ', lt: '< ', le: '≤ ', between: '', approx: '≈ ' }
const ORIGIN: Record<Assertion['origin'], string> = { text: 'из текста', user_answer: 'из ответа', user_edit: 'из правки', catalog: 'из каталога' }

function assertionValue(a: Assertion) {
  const unit = a.unit_raw && !a.value_raw.endsWith(a.unit_raw) ? ` ${a.unit_raw}` : ''
  return `${a.polarity === 'negative' ? 'не ' : ''}${OP[a.operator]}${a.value_raw}${unit}`
}

type Kind = 'problem' | 'ambiguous' | 'rejected' | 'text' | 'answer' | 'edit' | 'catalog'

const KIND: Record<Kind, [string, string]> = {
  text: ['Из текста', 'bg-sky-100 decoration-sky-500'],
  answer: ['Из ответа', 'bg-emerald-100 decoration-emerald-500'],
  edit: ['Правка', 'bg-violet-100 decoration-violet-500'],
  catalog: ['Каталог', 'bg-slate-200 decoration-slate-500'],
  ambiguous: ['Неоднозначно', 'bg-amber-100 decoration-amber-500'],
  problem: ['Ошибка', 'bg-red-100 decoration-red-500'],
  rejected: ['Отклонено', 'bg-slate-100 decoration-slate-400 line-through'],
}

function kindOf(a: Assertion, problems: Set<string>): Kind {
  if (a.status === 'rejected') return 'rejected'
  if (problems.has(a.id)) return 'problem'
  if (a.status === 'ambiguous') return 'ambiguous'
  return a.origin === 'user_answer' ? 'answer' : a.origin === 'user_edit' ? 'edit' : a.origin === 'catalog' ? 'catalog' : 'text'
}

const RANK: Kind[] = ['problem', 'ambiguous', 'text', 'answer', 'edit', 'catalog', 'rejected']

interface Segment {
  text: string
  ids: string[]
  fixes: Correction[]
  starts: Correction[] // corrections that begin here: their original spelling is shown before the segment
}

/** Corrections applied to the accepted text itself (layout and misspellings fixed before parsing). */
const textCorrections = (card: CardState) => card.corrections.filter((c) => c.target === 'text')

/** Splits `text` at assertion and correction span boundaries. Spans are Unicode code-point offsets, so slice an Array.from() copy. */
function segments(text: string, assertions: Assertion[], corrections: Correction[]): Segment[] {
  const cps = Array.from(text)
  const valid = (span: { start: number; end: number } | null | undefined) => !!span && span.start >= 0 && span.end <= cps.length && span.start < span.end
  const spans = assertions.flatMap((a) => [a.span, ...a.evidence.filter((e) => e.type === 'text').map((e) => e.span)].flatMap((span) => (span && valid(span) ? [{ id: a.id, start: span.start, end: span.end }] : [])))
  const fixes = corrections.filter((c) => valid(c.transformed_span))
  const cuts = [...new Set([0, cps.length, ...spans.flatMap((s) => [s.start, s.end]), ...fixes.flatMap((c) => [c.transformed_span!.start, c.transformed_span!.end])])].sort((x, y) => x - y)
  const out: Segment[] = []
  for (let i = 0; i < cuts.length - 1; i++) {
    const [a, b] = [cuts[i], cuts[i + 1]]
    out.push({
      text: cps.slice(a, b).join(''),
      ids: [...new Set(spans.filter((s) => s.start <= a && s.end >= b).map((s) => s.id))],
      fixes: fixes.filter((c) => c.transformed_span!.start <= a && c.transformed_span!.end >= b),
      starts: fixes.filter((c) => c.transformed_span!.start === a),
    })
  }
  return out
}

/** A span edit that puts the user's original spelling back. */
function revertFix(card: CardState, c: Correction): Fix | null {
  const span = c.transformed_span
  if (!span) return null
  const patch = {
    type: 'replace_span' as const,
    span: { start: span.start, end: span.end, text: span.text, text_revision: card.revision },
    replacement: c.original,
    snapshot_id: card.snapshot_id,
    base_revision: card.revision,
  }
  return { id: `revert:${span.start}`, label_ru: `Вернуть «${c.original}»`, patch, snapshot_id: card.snapshot_id, base_revision: card.revision }
}

/** Always-visible summary of what was corrected before the description was parsed. */
export function TextCorrections({ card, onFix, busy }: { card: CardState; onFix: (fix: Fix) => void; busy: boolean }) {
  const fixes = textCorrections(card)
  if (!fixes.length) return null
  const layout = fixes.find((c) => c.kind === 'layout')
  const typos = [...new Map(fixes.filter((c) => c.kind === 'typo').map((c) => [`${c.original}→${c.replacement}`, c])).values()]
  return (
    <div className="mb-3 border-l-4 border-emerald-500 bg-emerald-50 px-3 py-2 text-sm" aria-label="Исправления в тексте">
      <div className="font-medium text-emerald-900">Текст исправлен автоматически перед разбором</div>
      {layout && (
        <p className="mt-1 text-slate-700">
          Раскладка: <span className="text-slate-500 line-through">{layout.original}</span> → <span className="font-medium">{layout.replacement}</span>
        </p>
      )}
      {typos.length > 0 && (
        <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
          {typos.map((c) => {
            const fix = revertFix(card, c)
            return (
              <li key={`${c.original}→${c.replacement}`} className="inline-flex flex-wrap items-center gap-1.5">
                <span className="text-slate-500 line-through">{c.original}</span>→<span className="font-medium">{c.replacement}</span>
                {fix && (
                  <button type="button" disabled={busy} onClick={() => onFix(fix)} className="text-xs text-slate-500 underline hover:text-slate-800 disabled:opacity-50">
                    вернуть
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export function SourceText({ card, active, onSelect }: { card: CardState; active: Set<string>; onSelect: (ids: string[]) => void }) {
  const [hover, setHover] = useState<string[]>([])
  const text = card.text
  const byId = new Map(card.assertions.map((a) => [a.id, a]))
  const problems = new Set(card.diagnostics.filter((d) => d.severity !== 'info').flatMap((d) => d.assertion_ids))
  const fixes = textCorrections(card)
  const segs = segments(text, card.assertions, fixes)
  const focus = hover.length ? hover : [...active]
  const focused = focus.map((id) => byId.get(id)).filter((a): a is Assertion => !!a)
  const usedKinds = new Set(card.assertions.map((a) => kindOf(a, problems)))
  const dim = active.size > 0

  const segKind = (ids: string[]) => RANK.find((k) => ids.some((id) => kindOf(byId.get(id)!, problems) === k))!
  const describe = (ids: string[]) =>
    ids
      .map((id) => byId.get(id)!)
      .map((a) => `${a.property_raw} → ${assertionValue(a)}`)
      .join('\n')

  return (
    <div>
      <p className="bg-brand-soft/60 px-3 py-2 text-[15px] leading-7 break-words whitespace-pre-wrap text-slate-900">
        {segs.map((s, i) => (
          <Fragment key={i}>
            {s.starts.map((c) => (
              <s key={c.transformed_span!.start} className="mr-1 text-[13px] text-red-600/80 decoration-red-500/70" title="Исходное написание">
                {c.original}
              </s>
            ))}
            {s.ids.length === 0 ? (
              s.fixes.length ? (
                <FixedText key={i} seg={s} />
              ) : (
                <span key={i}>{s.text}</span>
              )
            ) : (
              <mark
                key={i}
                role="button"
                aria-pressed={s.ids.some((id) => active.has(id))}
                tabIndex={0}
                title={describe(s.ids)}
                onMouseEnter={() => setHover(s.ids)}
                onMouseLeave={() => setHover([])}
                onFocus={() => setHover(s.ids)}
                onBlur={() => setHover([])}
                onClick={() => onSelect(s.ids)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelect(s.ids)
                  }
                }}
                className={cn(
                  'cursor-pointer rounded-sm px-0.5 text-inherit underline decoration-2 underline-offset-4 transition-opacity',
                  KIND[segKind(s.ids)][1],
                  dim && !s.ids.some((id) => active.has(id)) && 'opacity-40',
                  s.ids.some((id) => active.has(id)) && 'ring-brand ring-2',
                )}
              >
                {s.fixes.length ? <FixedText seg={s} /> : s.text}
              </mark>
            )}
          </Fragment>
        ))}
      </p>
      {fixes.length > 0 && <p className="mt-1 text-xs text-slate-500">Зачёркнуто — исходное написание, зелёным подчёркнуто — исправленное слово, которое использовано при разборе.</p>}
      {card.normalized_text && card.normalized_text !== card.text && <p className="mt-1 text-xs text-slate-500">Для поиска исправлена раскладка: «{card.normalized_text}». Выделения относятся к исходному тексту.</p>}

      <div className="mt-2 min-h-6 text-sm" aria-live="polite">
        {focused.length > 0 ? (
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {focused.map((a) => (
              <span key={a.id}>
                <span className="text-slate-500">{a.property_raw}</span> → <b>{assertionValue(a)}</b>
                <span className="ml-1 text-xs text-slate-400">
                  {ORIGIN[a.origin]}
                  {a.note_ru ? ` · ${a.note_ru}` : ''}
                </span>
              </span>
            ))}
          </div>
        ) : (
          <span className="text-xs text-slate-600">Выберите выделенный фрагмент, чтобы увидеть распознанную характеристику.</span>
        )}
      </div>

      {card.assertions.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {card.assertions.map((a) => (
            <li key={a.id} className="max-w-full">
              <button
                type="button"
                aria-pressed={active.has(a.id)}
                onClick={() => onSelect([a.id])}
                onMouseEnter={() => setHover([a.id])}
                onMouseLeave={() => setHover([])}
                title={`${ORIGIN[a.origin]} · ${a.extractor}${a.span ? '' : ' · без привязки к тексту'}`}
                className={cn('min-h-11 max-w-full rounded px-2 py-2 text-left text-xs break-words underline decoration-2 underline-offset-2', KIND[kindOf(a, problems)][1], active.has(a.id) && 'ring-brand ring-2')}
              >
                {a.property_raw}: {assertionValue(a)}
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
        {RANK.filter((k) => usedKinds.has(k)).map((k) => (
          <span key={k} className="inline-flex items-center gap-1">
            <span className={cn('inline-block h-2.5 w-2.5 rounded-sm', KIND[k][1])} />
            {KIND[k][0]}
          </span>
        ))}
      </div>
    </div>
  )
}

function FixedText({ seg }: { seg: Segment }) {
  const was = [...new Set(seg.fixes.map((c) => c.original))].join(', ')
  return (
    <span title={`Исправлено автоматически. Было: «${was}»`} className="rounded-sm bg-emerald-100 underline decoration-emerald-600 decoration-wavy decoration-1 underline-offset-4">
      {seg.text}
    </span>
  )
}
