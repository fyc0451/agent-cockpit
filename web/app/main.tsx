import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { AuthGate } from '../features/AuthGate'
import { CapabilitiesProvider } from '../state/capabilities'
import { SelectionProvider } from '../state/selection'
import { ThemeProvider } from '../state/theme'
import '../styles/global.css'
import '../features/shell/dsw.css'

const queryClient = new QueryClient()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AuthGate>
          <HashRouter>
            <CapabilitiesProvider>
              <SelectionProvider>
                <App />
              </SelectionProvider>
            </CapabilitiesProvider>
          </HashRouter>
        </AuthGate>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
)
