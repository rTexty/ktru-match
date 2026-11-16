import { KtruPage } from '@/components/ktru/KtruPage'
import { createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/')({
  validateSearch: (search: Record<string, unknown>): { text?: string } => (typeof search.text === 'string' && search.text.trim() ? { text: search.text.slice(0, 4000) } : {}),
  component: Index,
})

function Index() {
  const { text } = Route.useSearch()
  const navigate = Route.useNavigate()
  return <KtruPage initialText={text} onInitialTextUsed={() => void navigate({ search: {}, replace: true })} />
}
