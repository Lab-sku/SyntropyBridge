import { Navigate, useLocation } from 'react-router-dom';
import { useAuthStore, selectIsAuthed, selectRole, selectReady } from '@/stores/authStore';
import { Loader2 } from 'lucide-react';

/**
 * Guards a route tree. Optionally pass a ``role`` prop to restrict
 * access to a specific role (e.g. ``<ProtectedRoute role="admin">``).
 *
 * Behaviour:
 *  - While the session is being bootstrapped: render a full-page loader.
 *  - If the user is not authenticated: redirect to /login and remember
 *    where they came from via ``state.from``.
 *  - If ``role`` is given and the current role doesn't match: send
 *    them to the right home for *their* role (so admins don't see
 *    user pages and vice versa).
 */
export default function ProtectedRoute({ children, role }) {
  const ready = useAuthStore(selectReady);
  const isAuthed = useAuthStore(selectIsAuthed);
  const currentRole = useAuthStore(selectRole);
  const location = useLocation();

  if (!ready) {
    return <FullPageLoader />;
  }
  if (!isAuthed) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  if (role && currentRole !== role) {
    return <Navigate to={currentRole === 'admin' ? '/admin' : '/chat'} replace />;
  }
  return children;
}

export function FullPageLoader() {
  return (
    <div className="flex h-screen items-center justify-center bg-ink-50">
      <Loader2 size={22} className="animate-spin text-ink-500" />
    </div>
  );
}
