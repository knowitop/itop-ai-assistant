import '@mantine/core/styles.css';
import { i18nReady } from './i18n';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';

i18nReady.then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
