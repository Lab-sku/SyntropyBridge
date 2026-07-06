import React, { Suspense, lazy, useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { useTheme } from './hooks/useTheme';
import { useLanguage } from './hooks/useLanguage';
import { useAuthStore, selectIsAuthed, selectRole, selectReady } from './stores/authStore';
import { Toaster } from 'sonner';

import AppShell from './components/AppShell';
import ErrorBoundary from './components/ErrorBoundary';
import ProtectedRoute, { FullPageLoader } from './components/ProtectedRoute';

// Lightweight pages — loaded eagerly so the first paint is instant.
import Login from './pages/Login';
import AdminLogin from './pages/AdminLogin';
import NotFound from './pages/NotFound';
import ForgotPassword from './pages/ForgotPassword';
import ResetPassword from './pages/ResetPassword';

// Heavy pages — lazy-loaded so the initial bundle stays small.
const Chat = lazy(() => import('./pages/Chat'));
const AdminDashboard = lazy(() => import('./pages/AdminDashboard'));
const Providers = lazy(() => import('./pages/Providers'));
const CustomProviders = lazy(() => import('./pages/CustomProviders'));
const Users = lazy(() => import('./pages/Users'));
const Subscriptions = lazy(() => import('./pages/Subscriptions'));
const Billing = lazy(() => import('./pages/Billing'));
const Logs = lazy(() => import('./pages/Logs'));
const Usage = lazy(() => import('./pages/Usage'));
const Wallet = lazy(() => import('./pages/Wallet'));
const ApiKeys = lazy(() => import('./pages/ApiKeys'));
const Account = lazy(() => import('./pages/Account'));
const AccountKey = lazy(() => import('./pages/AccountKey'));
const RedeemCodes = lazy(() => import('./pages/RedeemCodes'));
const Pricing = lazy(() => import('./pages/Pricing'));
const ModelPool = lazy(() => import('./pages/ModelPool'));

// Phase 9 admin pages (built in parallel by different agents)
const AdminOrders = lazy(() => import('./pages/AdminOrders'));
const AdminWalletAdjust = lazy(() => import('./pages/AdminWalletAdjust'));
const AdminAuditLogs = lazy(() => import('./pages/AdminAuditLogs'));
const AdminPlans = lazy(() => import('./pages/AdminPlans'));
const AdminPromoCodes = lazy(() => import('./pages/AdminPromoCodes'));
const AdminSettings = lazy(() => import('./pages/AdminSettings'));
const Channels = lazy(() => import('./pages/Channels'));
const Help = lazy(() => import('./pages/Help'));
const UserIntegrationGuide = lazy(() => import('./components/UserIntegrationGuide'));
const HelpButton = lazy(() => import('./components/HelpButton'));
const OnboardingTour = lazy(() => import('./components/OnboardingTour'));

function LangSync() {
  const { lang } = useLanguage();
  const location = useLocation();
  useEffect(() => {
    if (lang) document.documentElement.lang = lang;
  }, [lang]);
  return null;
}

/**
 * Blocks already-logged-in users from the /login page, redirecting
 * them to the appropriate home for their role.
 */
function PublicOnlyRoute({ children }) {
  const isAuthed = useAuthStore(selectIsAuthed);
  const role = useAuthStore(selectRole);
  if (isAuthed) {
    return <Navigate to={role === 'admin' ? '/admin' : '/chat'} replace />;
  }
  return children;
}

function RootRedirect() {
  const isAuthed = useAuthStore(selectIsAuthed);
  const role = useAuthStore(selectRole);
  return <Navigate to={isAuthed ? (role === 'admin' ? '/admin' : '/chat') : '/login'} replace />;
}

export default function App() {
  const { resolvedTheme } = useTheme();
  // Subscribe to *each* field individually so the component only
  // re-renders when one of them actually changes.
  const ready = useAuthStore(selectReady);
  const isAuthed = useAuthStore(selectIsAuthed);
  const checkSession = useAuthStore((s) => s.checkSession);

  // Bootstrap the auth state on mount. Even if bootstrapFromCache()
  // set ``ready: true`` with a cached session, we *always* verify
  // against the backend — the cache may be stale (cookie expired,
  // user deactivated, etc.).
  useEffect(() => {
    checkSession().catch(() => {
      // checkSession swallows errors internally; this catch is just
      // defence in depth.
    });
  }, [checkSession]);

  if (!ready) {
    return <FullPageLoader />;
  }

  return (
    <ErrorBoundary>
      <BrowserRouter>
        <LangSync />
        <div
          className="min-h-screen bg-white text-ink-900 dark:bg-ink-950 dark:text-ink-100"
          data-theme={resolvedTheme}
        >
          <Toaster
            position="top-right"
            theme={resolvedTheme}
            richColors
            closeButton
            toastOptions={{
              style: { fontSize: '12.5px' },
            }}
          />
          <Suspense fallback={<FullPageLoader />}>
            <OnboardingTour />
            <HelpButton />
            <Routes>
            {/* Public */}
            <Route path="/help" element={<Help />} />
            <Route
              path="/login"
              element={
                <PublicOnlyRoute>
                  <Login />
                </PublicOnlyRoute>
              }
            />
            <Route
              path="/admin/login"
              element={
                <PublicOnlyRoute>
                  <AdminLogin />
                </PublicOnlyRoute>
              }
            />
            <Route path="/forgot-password" element={<ForgotPassword />} />
            <Route path="/reset-password" element={<ResetPassword />} />

            {/* Chat has its own dedicated sidebar (logo + new-chat +
                search + conversation history + model picker + logout),
                so it renders standalone — NOT inside AppShell, which
                would double-stack two sidebars horizontally. */}
            <Route
              path="/chat"
              element={
                <ProtectedRoute>
                  <Chat />
                </ProtectedRoute>
              }
            />

            {/* Protected (any role) — wrapped in AppShell for the
                standard top-bar + app-sidebar layout. */}
            <Route
              element={
                <ProtectedRoute>
                  <AppShell />
                </ProtectedRoute>
              }
            >
              <Route path="/usage" element={<Usage />} />
              <Route path="/wallet" element={<Wallet />} />
              <Route path="/account" element={<Account />} />
              <Route path="/account/key" element={<AccountKey />} />
              <Route path="/integration" element={<UserIntegrationGuide />} />
              <Route path="/model-pool" element={<ModelPool />} />
            </Route>

            {/* Admin-only */}
            <Route
              path="/admin"
              element={
                <ProtectedRoute role="admin">
                  <AppShell />
                </ProtectedRoute>
              }
            >
              <Route index element={<AdminDashboard />} />
              <Route path="providers" element={<Providers />} />
              <Route path="custom-providers" element={<CustomProviders />} />
              <Route path="users" element={<Users />} />
              <Route path="subscriptions" element={<Subscriptions />} />
              <Route path="billing" element={<Billing />} />
              <Route path="logs" element={<Logs />} />
              <Route path="api-keys" element={<ApiKeys />} />
              <Route path="redeem-codes" element={<RedeemCodes />} />
              <Route path="pricing" element={<Pricing />} />
              <Route path="orders" element={<AdminOrders />} />
              <Route path="wallet-adjust" element={<AdminWalletAdjust />} />
              <Route path="audit-logs" element={<AdminAuditLogs />} />
              <Route path="plans" element={<AdminPlans />} />
              <Route path="promo-codes" element={<AdminPromoCodes />} />
              <Route path="channels" element={<Channels />} />
              <Route path="settings" element={<AdminSettings />} />
            </Route>

            {/* Fallback */}
            <Route path="/" element={<RootRedirect />} />
            <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
        </div>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
