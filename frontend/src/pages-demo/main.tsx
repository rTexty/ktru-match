import { createRoot } from 'react-dom/client'
import scenarioUrl from '../../pages/scenario.json?url'
import { Demo, type DemoScenario } from './Demo'
import './demo.css'

const root = createRoot(document.getElementById('root')!)

async function start() {
  try {
    const response = await fetch(scenarioUrl)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const { scenarios } = await response.json() as { scenarios: DemoScenario[] }
    if (!scenarios.length) throw new Error('Empty demo')
    root.render(<Demo scenarios={scenarios} />)
  } catch {
    root.render(
      <div className="mx-auto max-w-xl p-6">
        <p role="alert">Не удалось загрузить примеры. Обновите страницу, чтобы попробовать ещё раз.</p>
      </div>,
    )
  }
}

void start()
