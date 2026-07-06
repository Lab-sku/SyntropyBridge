import { useCallback, useEffect, useRef, useState } from 'react';

const STORAGE_KEY = 'mm:wallet:balance';
const LOW_THRESHOLD = 100;
const DEBOUNCE_MS = 300;

/**
 * Parse the localStorage balance value.
 * Returns { balance, at } or null if missing / unreadable.
 */
function readBalance() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.balance === 'number' && isFinite(parsed.balance)) {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Hook that returns the current wallet balance and whether it is low.
 *
 * - Reads from localStorage key `mm:wallet:balance` (JSON { balance, at }).
 * - Listens to cross-tab `storage` events so the value stays fresh.
 * - Debounces re-renders to avoid spam from rapid storage writes.
 * - If the key is absent or unreadable, returns `{ isLow: false, balance: 0 }`.
 *
 * The Wallet page already writes to this key on every poll cycle (30 s)
 * and on cross-tab sync, so this hook piggy-backs on that existing
 * mechanism without making any extra API calls.
 */
export default function useBalanceWarning() {
  const [state, setState] = useState(() => {
    const data = readBalance();
    return {
      balance: data ? data.balance : 0,
      isLow: data ? data.balance < LOW_THRESHOLD : false,
    };
  });

  // Debounce timer ref — shared across storage event handler and cleanup.
  const timerRef = useRef(null);

  const syncFromStorage = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      const data = readBalance();
      const balance = data ? data.balance : 0;
      const isLow = data ? data.balance < LOW_THRESHOLD : false;
      setState((prev) => {
        // Skip re-render when nothing changed
        if (prev.balance === balance && prev.isLow === isLow) return prev;
        return { balance, isLow };
      });
    }, DEBOUNCE_MS);
  }, []);

  // Listen for cross-tab storage events.
  useEffect(() => {
    const onStorage = (ev) => {
      if (ev.key !== STORAGE_KEY) return;
      syncFromStorage();
    };
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('storage', onStorage);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [syncFromStorage]);

  // Also listen for same-tab writes via the custom event that some
  // browsers don't fire natively (Safari). We dispatch this in the
  // Wallet page's polling loop indirectly via localStorage.setItem.
  // Since same-tab `storage` events are not guaranteed, we poll the
  // key at a slow interval as a fallback (every 60 s).
  useEffect(() => {
    const id = setInterval(syncFromStorage, 60_000);
    return () => clearInterval(id);
  }, [syncFromStorage]);

  return state;
}
