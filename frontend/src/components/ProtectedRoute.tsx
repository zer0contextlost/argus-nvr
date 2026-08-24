/**
 * ProtectedRoute — wraps routes that require authentication.
 * If the user is not authenticated, redirects to /login preserving the
 * originally requested location so the user lands there after login.
 */

import { Navigate, useLocation } from 'react-router-dom'
import { isAuthenticated, mustChangePassword } from '../api/client'

interface Props {
  children: React.ReactNode
}

export function ProtectedRoute({ children }: Props) {
  const location = useLocation()

  if (!isAuthenticated()) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }

  if (mustChangePassword() && location.pathname !== '/change-password') {
    return <Navigate to="/change-password" replace />
  }

  return <>{children}</>
}
