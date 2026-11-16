import { BatchPage } from '@/components/pages/BatchPage'
import { createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/batch')({ component: BatchPage })
