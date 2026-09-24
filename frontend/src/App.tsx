import { Suspense, lazy } from 'react'
import { BrowserRouter, Navigate, Routes, Route } from 'react-router-dom'
import { AuthProvider } from './auth/session'
import { RequireAuth } from './auth/guards'
import { Navbar } from './components/layout/Navbar'
import Login from './pages/Login'
import Register from './pages/Register'
import ForgotPassword from './pages/ForgotPassword'
import ResetPassword from './pages/ResetPassword'
import VerifyEmail from './pages/VerifyEmail'
import Problems from './pages/Problems'
import Settings from './pages/Settings'

// Lazy-loaded: Workspace pulls in Monaco, KaTeX, and the syntax highlighter --
// together the single biggest chunk of the app's JS. Splitting it into its own
// chunk keeps those out of every other route's initial load (auth, the problems
// list), fetching them only when a user actually opens a problem.
const Workspace = lazy(() => import('./pages/workspace/Workspace'))

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <div className="flex min-h-full flex-col">
          <Navbar />
          <main className="flex-1">
            <Routes>
              {/* No marketing page: a self-hosted instance's visitors already know why they're
                  here. RequireAuth on /problems sends a signed-out visitor on to /login. */}
              <Route path="/" element={<Navigate to="/problems" replace />} />
              <Route path="/login" element={<Login />} />
              <Route path="/register" element={<Register />} />
              <Route path="/forgot-password" element={<ForgotPassword />} />
              <Route path="/reset-password" element={<ResetPassword />} />
              <Route path="/verify-email" element={<VerifyEmail />} />
              <Route
                path="/settings"
                element={
                  <RequireAuth>
                    <Settings />
                  </RequireAuth>
                }
              />
              <Route
                path="/problems"
                element={
                  <RequireAuth>
                    <Problems />
                  </RequireAuth>
                }
              />
              <Route
                path="/problems/:slug"
                element={
                  <RequireAuth>
                    <Suspense
                      fallback={<div className="p-6 text-sm text-zinc-500">Loading…</div>}
                    >
                      <Workspace />
                    </Suspense>
                  </RequireAuth>
                }
              />
            </Routes>
          </main>
        </div>
      </AuthProvider>
    </BrowserRouter>
  )
}
