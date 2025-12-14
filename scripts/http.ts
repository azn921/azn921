import fetch, { type RequestInit, type Response } from "node-fetch";

export type FetchResult = {
  response: Response;
  finalUrl: string;
  redirectsFollowed: number;
};

export async function fetchFollow(
  url: string,
  init?: RequestInit & { maxRedirects?: number }
): Promise<FetchResult> {
  const maxRedirects = init?.maxRedirects ?? 10;
  const { maxRedirects: _ignored, ...fetchInit } = init ?? {};

  // node-fetch will follow redirects automatically when redirect='follow'
  const response = await fetch(url, { ...fetchInit, redirect: "follow" });
  const finalUrl = response.url || url;

  // We can't directly know how many redirects node-fetch followed without manual mode.
  // Provide a best-effort 0/unknown count by comparing URLs.
  const redirectsFollowed = finalUrl === url ? 0 : 1;

  // Protect against crazy redirect chains by re-fetching in manual mode when url changed
  // to estimate redirects (best effort).
  if (redirectsFollowed === 1) {
    let current = url;
    let count = 0;
    for (let i = 0; i < maxRedirects; i++) {
      const r = await fetch(current, { ...fetchInit, redirect: "manual", method: fetchInit.method ?? "GET" });
      if (r.status >= 300 && r.status < 400) {
        const loc = r.headers.get("location");
        if (!loc) break;
        const next = new URL(loc, current).toString();
        count += 1;
        current = next;
        continue;
      }
      break;
    }
    return { response, finalUrl, redirectsFollowed: count };
  }

  return { response, finalUrl, redirectsFollowed };
}

