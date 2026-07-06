import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './i18n'; // must run before the first <App/> render so translations are ready
import './styles/index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// L15: Register the PWA service worker in production only. Dev server
// serves /sw.js from public/ so it can be tested, but Vite HMR + SW
// caching would interfere during development.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // SW registration failure is non-fatal — the app still works
      // as a regular web page.
    });
  });
}
