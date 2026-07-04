import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import cs from './locales/cs.json';
import de from './locales/de.json';
import en from './locales/en.json';
import es from './locales/es.json';
import fr from './locales/fr.json';
import it from './locales/it.json';
import kk from './locales/kk.json';
import pl from './locales/pl.json';
import ru from './locales/ru.json';
import sk from './locales/sk.json';
import uk from './locales/uk.json';
import zh from './locales/zh.json';

i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    ru: { translation: ru },
    zh: { translation: zh },
    es: { translation: es },
    fr: { translation: fr },
    it: { translation: it },
    pl: { translation: pl },
    uk: { translation: uk },
    kk: { translation: kk },
    de: { translation: de },
    cs: { translation: cs },
    sk: { translation: sk },
  },
  lng: localStorage.getItem('locale') ?? 'en',
  fallbackLng: 'en',
  interpolation: { escapeValue: false },
});

export default i18n;
