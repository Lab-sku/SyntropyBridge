import { useCallback, useEffect, useMemo, useState } from 'react';

const STORAGE_KEY = 'app_theme'; // 'light' | 'dark' | 'system'
const DOM_ATTR = 'data-theme'; // mirrors onto <html data-theme="light|dark">

/**
 * Read the persisted theme choice. The string 'system' is a real value
 * here — when set, we fall back to `prefers-color-scheme`. Anything
 * else is treated as unset and we default to 'system'.
 */
function getInitialTheme() {
  if (typeof window === 'undefined') return 'system';
  const saved = window.localStorage?.getItem(STORAGE_KEY);
  if (saved === 'light' || saved === 'dark' || saved === 'system') return saved;
  return 'system';
}

function readSystemTheme() {
  if (typeof window === 'undefined') return 'light';
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/**
 * Apply the resolved theme to <html>. We do this as a *side effect* and
 * intentionally avoid setting the attribute when the choice resolves to
 * the same value as what is already on the document — this prevents a
 * flash of the wrong theme when the user opens multiple tabs.
 *
 * Tailwind's `darkMode: 'class'` looks for the `.dark` class on <html>,
 * so we mirror both the class and the data attribute.
 */
function applyToDom(theme) {
  if (typeof document === 'undefined') return;
  const el = document.documentElement;
  if (theme === 'light' || theme === 'dark') {
    if (el.getAttribute(DOM_ATTR) !== theme) el.setAttribute(DOM_ATTR, theme);
    el.classList.toggle('dark', theme === 'dark');
  } else {
    // 'system' → let the media query in CSS pick the colours
    if (el.hasAttribute(DOM_ATTR)) el.removeAttribute(DOM_ATTR);
    el.classList.remove('dark');
  }
  // Helpful for components that paint based on theme without re-rendering
  el.style.colorScheme = theme === 'light' ? 'light' : 'dark';
}

export function useTheme() {
  const [theme, setThemeState] = useState(() => getInitialTheme());
  const [systemTheme, setSystemTheme] = useState(() => readSystemTheme());

  const resolvedTheme = useMemo(() => {
    if (theme === 'light' || theme === 'dark') return theme;
    return systemTheme;
  }, [theme, systemTheme]);

  // Reflect the resolved theme onto the <html> element whenever it
  // changes (either because the user picked something explicit, or
  // because the OS colour scheme flipped).
  useEffect(() => {
    applyToDom(resolvedTheme);
  }, [resolvedTheme]);

  // Subscribe to OS theme changes so the UI auto-updates when the
  // choice is 'system'. We tear down the listener on unmount.
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined;
    const mql = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = (e) => setSystemTheme(e.matches ? 'dark' : 'light');
    // Older Safari uses `addListener`; modern browsers use `addEventListener`.
    if (mql.addEventListener) {
      mql.addEventListener('change', handler);
      return () => mql.removeEventListener('change', handler);
    }
    mql.addListener(handler);
    return () => mql.removeListener(handler);
  }, []);

  // Cross-instance / cross-tab sync: when another useTheme instance (or
  // another browser tab) writes to localStorage, this instance reads
  // the new value back into its own state. Without this, each component
  // that calls useTheme() has its own isolated useState, so toggling
  // the theme in one place updates the DOM attribute but not the
  // resolvedTheme value other components depend on.
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onStorage = (e) => {
      if (e.key !== STORAGE_KEY) return;
      const next = e.newValue;
      if (next === 'light' || next === 'dark' || next === 'system') {
        setThemeState(next);
      } else if (next === null) {
        setThemeState('system');
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const setTheme = useCallback((next) => {
    if (next !== 'light' && next !== 'dark' && next !== 'system') return;
    setThemeState(next);
    let newValue = null;
    try {
      if (next) {
        window.localStorage?.setItem(STORAGE_KEY, next);
        newValue = next;
      } else {
        window.localStorage?.removeItem(STORAGE_KEY);
      }
    } catch {
      // localStorage may be unavailable (private mode, quota, etc.) —
      // the in-memory state is still consistent for this tab.
    }
    // Broadcast to other useTheme() instances in THIS tab (native
    // `storage` events only fire for cross-tab writes).
    try {
      window.dispatchEvent(
        new StorageEvent('storage', { key: STORAGE_KEY, newValue }),
      );
    } catch {
      // StorageEvent constructor may be unavailable in very old browsers —
      // the writing instance is still correct; others will re-sync on
      // next mount.
    }
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(resolvedTheme === 'dark' ? 'light' : 'dark');
  }, [resolvedTheme, setTheme]);

  return { theme, resolvedTheme, setTheme, toggleTheme };
}
