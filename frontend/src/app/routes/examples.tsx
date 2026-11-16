import { ExamplesPage } from '@/components/pages/ExamplesPage'
import { createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/examples')({ component: ExamplesPage })
