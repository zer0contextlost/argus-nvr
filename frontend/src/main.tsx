import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
// @ts-expect-error css import
import './styles/theme.css'
import { ToastProvider } from './components/ui/Toast'
import { Shell } from './components/Shell'
import { ProtectedRoute } from './components/ProtectedRoute'
import { LoginPage } from './pages/LoginPage'
import { CamerasPage } from './pages/CamerasPage'
import { LivePage } from './pages/LivePage'
import { EventsPage } from './pages/EventsPage'
import { PlaybackPage } from './pages/PlaybackPage'
import { SettingsPage } from './pages/SettingsPage'
import { HealthPage } from './pages/HealthPage'
import { PersonsPage } from './pages/PersonsPage'
import { ChangePasswordPage } from './pages/ChangePasswordPage'
import { DashboardPage } from './pages/DashboardPage'
import { UsersPage } from './pages/UsersPage'
import { AuditPage } from './pages/AuditPage'
import { SearchPage } from './pages/SearchPage'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <Routes>
          {/* Public routes */}
          <Route path="/login" element={<LoginPage />} />

          {/* Forced password change — protected (must be logged in) but outside Shell */}
          <Route
            path="/change-password"
            element={
              <ProtectedRoute>
                <ChangePasswordPage />
              </ProtectedRoute>
            }
          />

          {/* Protected shell — all child routes require authentication */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Shell />
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<DashboardPage />} />
            <Route path="live" element={<LivePage />} />
            <Route path="cameras" element={<CamerasPage />} />
            <Route path="playback" element={<PlaybackPage />} />
            <Route path="events" element={<EventsPage />} />
            <Route path="persons" element={<PersonsPage />} />
            <Route path="users" element={<UsersPage />} />
            <Route path="audit" element={<AuditPage />} />
            <Route path="search" element={<SearchPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="health" element={<HealthPage />} />
          </Route>
        </Routes>
      </ToastProvider>
    </BrowserRouter>
  </React.StrictMode>,
)
