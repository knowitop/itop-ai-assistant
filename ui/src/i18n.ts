import i18n, { Resource } from 'i18next';
import { initReactI18next } from 'react-i18next';

// Vite resolves the glob at build time, so the set of bundles is known
// statically. A plain `import(`./locales/${lang}.json`)` would throw "Unknown
// variable dynamic import" in the built app for any code without a file —
// a region tag like "ru-RU", or a value left in localStorage by something else.
const bundles = import.meta.glob<{ default: Record<string, unknown> }>('./locales/*.json');

const PREFIX = './locales/';
const SUFFIX = '.json';
const FALLBACK = 'en';

export const AVAILABLE_LANGUAGES = Object.keys(bundles)
  .map((path) => path.slice(PREFIX.length, -SUFFIX.length))
  .sort();

/** "ru-RU" → "ru"; anything we have no bundle for → "en". */
export function resolveLanguage(lang: string | null | undefined): string {
  const code = (lang ?? '').toLowerCase().split('-')[0];
  return AVAILABLE_LANGUAGES.includes(code) ? code : FALLBACK;
}

async function loadBundle(lang: string): Promise<Record<string, unknown>> {
  const module = await bundles[`${PREFIX}${lang}${SUFFIX}`]();
  return module.default;
}

const stored = localStorage.getItem('locale');
const initialLang = resolveLanguage(stored);
// Rewrite an unusable stored value so the fallback is not re-resolved forever.
if (stored !== null && stored !== initialLang) localStorage.setItem('locale', initialLang);

// English ships alongside the chosen language: fallbackLng only reaches a key
// whose bundle is already loaded, so without this a key missing from a
// translation would render as its raw id ("nav.setup") instead of English.
async function initialResources(): Promise<Resource> {
  if (initialLang === FALLBACK) return { [FALLBACK]: { translation: await loadBundle(FALLBACK) } };
  const [translation, fallback] = await Promise.all([loadBundle(initialLang), loadBundle(FALLBACK)]);
  return { [initialLang]: { translation }, [FALLBACK]: { translation: fallback } };
}

export const i18nReady = initialResources().then((resources) =>
  i18n.use(initReactI18next).init({
    resources,
    lng: initialLang,
    fallbackLng: FALLBACK,
    // An empty string is a missing translation, not a deliberate blank label.
    returnEmptyString: false,
    interpolation: { escapeValue: false },
  })
);

/** Loads the bundle if needed and returns the code actually available. */
export async function loadLanguage(lang: string): Promise<string> {
  const code = resolveLanguage(lang);
  if (i18n.hasResourceBundle(code, 'translation')) return code;
  i18n.addResourceBundle(code, 'translation', await loadBundle(code));
  return code;
}

export default i18n;
