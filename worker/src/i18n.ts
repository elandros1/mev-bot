/**
 * Internationalization helper.
 * Loads the English locale bundled at build time.
 */
import enLocale from "./locales/en.json";

export type Locale = typeof enLocale;

/** Get a string by dotted key path (e.g. t("card.alert_title")). */
export function t(dottedKey: string): string {
  const parts = dottedKey.split(".");
  let cur: unknown = enLocale;
  for (const p of parts) {
    if (cur && typeof cur === "object" && p in (cur as Record<string, unknown>)) {
      cur = (cur as Record<string, unknown>)[p];
    } else {
      return dottedKey; // fallback: return the key itself
    }
  }
  return typeof cur === "string" ? cur : dottedKey;
}
