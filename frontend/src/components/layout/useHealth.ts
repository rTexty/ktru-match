import { ktruApi } from '@/api/ktru'
import { useQuery } from '@tanstack/react-query'

export function useHealth() {
  return useQuery({ queryKey: ['ktru', 'health'], queryFn: ktruApi.health, refetchInterval: 30_000, retry: false })
}
