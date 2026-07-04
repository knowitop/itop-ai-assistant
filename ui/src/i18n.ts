import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

const initialLang = localStorage.getItem('locale') ?? 'en';

export const i18nReady = import(`./locales/${initialLang}.json`).then((module) =>
  i18n.use(initReactI18next).init({
    resources: { [initialLang]: { translation: module.default } },
    lng: initialLang,
    fallbackLng: 'en',
    interpolation: { escapeValue: false },
  })
);

export async function loadLanguage(lang: string) {
  if (i18n.hasResourceBundle(lang, 'translation')) return;
  const module = await import(`./locales/${lang}.json`);
  i18n.addResourceBundle(lang, 'translation', module.default);
}

export default i18n;
