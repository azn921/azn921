export type NormalizedUrl = {
  input: string;
  normalized: string | null;
  siteRoot: string | null;
  reason?: string;
};

const STRIP_QUERY_KEYS = [
  /^utm_/i,
  /^fbclid$/i,
  /^gclid$/i,
  /^mc_cid$/i,
  /^mc_eid$/i,
  /^mkt_tok$/i,
  /^ref$/i,
];

export function normalizeUrl(input: string): NormalizedUrl {
  const raw = (input ?? "").trim();
  if (!raw) return { input, normalized: null, siteRoot: null, reason: "empty" };
  if (/^mailto:/i.test(raw)) return { input, normalized: null, siteRoot: null, reason: "mailto" };
  if (/^javascript:/i.test(raw)) return { input, normalized: null, siteRoot: null, reason: "javascript" };

  let asString = raw;
  if (asString.startsWith("//")) asString = `https:${asString}`;
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(asString)) asString = `https://${asString}`;

  let url: URL;
  try {
    url = new URL(asString);
  } catch {
    return { input, normalized: null, siteRoot: null, reason: "invalid_url" };
  }

  if (!/^https?:$/.test(url.protocol)) {
    return { input, normalized: null, siteRoot: null, reason: "non_http" };
  }

  url.hash = "";
  url.username = "";
  url.password = "";

  // Normalize hostname casing + strip default ports
  url.hostname = url.hostname.toLowerCase();
  if ((url.protocol === "https:" && url.port === "443") || (url.protocol === "http:" && url.port === "80")) {
    url.port = "";
  }

  // Strip common tracking params
  const toDelete: string[] = [];
  for (const [k] of url.searchParams.entries()) {
    if (STRIP_QUERY_KEYS.some((re) => re.test(k))) toDelete.push(k);
  }
  for (const k of toDelete) url.searchParams.delete(k);

  // Normalize path: collapse multiple slashes, drop trailing slash unless root
  url.pathname = url.pathname.replace(/\/{2,}/g, "/");
  if (url.pathname.length > 1) url.pathname = url.pathname.replace(/\/+$/g, "");

  const normalized = url.toString();
  const siteRoot = `${url.protocol}//${url.host}`;

  return { input, normalized, siteRoot };
}

export function decodeHtmlEntities(text: string): string {
  return text
    .replaceAll("&amp;", "&")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">");
}

