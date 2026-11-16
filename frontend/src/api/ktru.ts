import type { CardState, EditPatch, ErrorBody, Example, ExampleDomain, Health, PositionRef, ProductPhoto, Value } from './ktru.generated'
export type * from './ktru.generated'
export { MAX_SPLIT_TEXT, MAX_TEXT } from './ktru.generated'

export class KtruApiError extends Error {
  status: number
  body: ErrorBody

  constructor(status: number, body: ErrorBody) {
    super(body.message_ru)
    this.status = status
    this.body = body
  }
}

const BASE = (import.meta.env.VITE_API_URL ?? '/api') + '/ktru'

async function request<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE + path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new KtruApiError(0, { code: 'unavailable', message_ru: 'Сервер недоступен. Проверьте соединение.', retryable: true, current_revision: null, request_id: null })
  }
  const data: unknown = await res.json().catch(() => null)
  if (!res.ok) {
    const err = (data as { error?: ErrorBody } | null)?.error
    throw new KtruApiError(
      res.status,
      err ?? {
        code: res.status === 404 ? 'not_found' : res.status === 422 ? 'invalid_input' : 'unavailable',
        message_ru: `Ошибка сервера (${res.status})`,
        retryable: res.status >= 500,
        current_revision: null,
        request_id: null,
      },
    )
  }
  return data as T
}

// crypto.randomUUID exists only in secure contexts; the app is also served over plain http (e.g. Tailscale IP).
const rid = () => (typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join(''))

export const ktruApi = {
  health: () => request<Health>('GET', '/health'),
  split: (text: string) => request<string[]>('POST', '/split', { text }),
  photos: (ref: PositionRef, text: string) => request<ProductPhoto[]>('GET', `/positions/${encodeURIComponent(ref.code)}/photos?${new URLSearchParams({ snapshot_id: ref.snapshot_id, version: String(ref.version), text: Array.from(text).slice(0, 500).join('') })}`),
  examples: (domain: ExampleDomain, n: number) => request<Example[]>('GET', `/examples?domain=${domain}&n=${n}`),
  session: (id: string) => request<CardState>('GET', `/sessions/${encodeURIComponent(id)}`),
  parse: (text: string, session?: { session_id: string; revision: number }) =>
    request<CardState>('POST', '/parse', {
      ...(session ? { session_id: session.session_id } : {}),
      request_id: rid(),
      expected_revision: session?.revision ?? 0,
      text,
    }),
  analyze: (s: { session_id: string; revision: number }) => request<CardState>('POST', '/analyze', { session_id: s.session_id, request_id: rid(), expected_revision: s.revision }),
  answer: (s: { session_id: string; revision: number }, question_id: string, option_id: string) =>
    request<CardState>('POST', '/answer', { session_id: s.session_id, request_id: rid(), expected_revision: s.revision, question_id, option_id }),
  answerValue: (s: { session_id: string; revision: number }, question_id: string, value: Value) => request<CardState>('POST', '/answer', { session_id: s.session_id, request_id: rid(), expected_revision: s.revision, question_id, value }),
  edit: (s: { session_id: string; revision: number }, patch: EditPatch) => request<CardState>('POST', '/edit', { session_id: s.session_id, request_id: rid(), expected_revision: s.revision, patch }),
  undo: (s: { session_id: string; revision: number }) => request<CardState>('POST', '/undo', { session_id: s.session_id, request_id: rid(), expected_revision: s.revision }),
}
