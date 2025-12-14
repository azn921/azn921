export type CongressGovQueryValue = string | number | boolean | null | undefined;

export type CongressGovQuery = Record<string, CongressGovQueryValue | CongressGovQueryValue[]>;

export type CongressGovClientOptions = {
  /**
   * Override the default base URL (rarely needed).
   * Default: https://api.congress.gov/v3
   */
  baseUrl?: string;
  /**
   * API key for Congress.gov v3. If omitted, reads from process.env.CONGRESS_GOV_API_KEY.
   */
  apiKey?: string;
};

export class CongressGovError extends Error {
  readonly status: number;
  readonly url: string;
  readonly bodyText?: string;

  constructor(message: string, opts: { status: number; url: string; bodyText?: string }) {
    super(message);
    this.name = "CongressGovError";
    this.status = opts.status;
    this.url = opts.url;
    this.bodyText = opts.bodyText;
  }
}

function isSafeEndpoint(endpoint: string): boolean {
  // Allow only path-like endpoints (no scheme/host, no whitespace).
  if (!endpoint) return false;
  if (endpoint.includes("://")) return false;
  if (/[\r\n\t ]/.test(endpoint)) return false;
  // Require leading slash and only permit conservative characters.
  if (!endpoint.startsWith("/")) return false;
  return /^\/[A-Za-z0-9/_-]*$/.test(endpoint);
}

function toSearchParams(query: CongressGovQuery): URLSearchParams {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item === undefined || item === null) continue;
        sp.append(k, String(item));
      }
      continue;
    }
    if (v === undefined || v === null) continue;
    sp.set(k, String(v));
  }
  return sp;
}

function normalizeEndpoint(endpoint: string): string {
  const trimmed = endpoint.trim();
  const withLeadingSlash = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withLeadingSlash.replace(/\/{2,}/g, "/");
}

export function createCongressGovClient(options: CongressGovClientOptions = {}) {
  const baseUrl = (options.baseUrl ?? process.env.CONGRESS_GOV_API_BASE_URL ?? "https://api.congress.gov/v3").replace(
    /\/+$/,
    "",
  );
  const apiKey = options.apiKey ?? process.env.CONGRESS_GOV_API_KEY;

  async function fetchJson<T = unknown>(args: {
    endpoint: string;
    query?: CongressGovQuery;
    signal?: AbortSignal;
    /**
     * Defaults to "no-store" to avoid caching potentially sensitive queries.
     * You can override for stable endpoints.
     */
    cache?: RequestCache;
  }): Promise<T> {
    if (!apiKey) {
      throw new Error("Missing Congress.gov API key (set CONGRESS_GOV_API_KEY).");
    }

    const endpoint = normalizeEndpoint(args.endpoint);
    if (!isSafeEndpoint(endpoint)) {
      throw new Error(`Invalid endpoint: ${args.endpoint}`);
    }

    const query: CongressGovQuery = {
      format: "json",
      ...args.query,
      // Force server-side key usage.
      api_key: apiKey,
    };

    const url = `${baseUrl}${endpoint}?${toSearchParams(query).toString()}`;

    const res = await fetch(url, {
      method: "GET",
      cache: args.cache ?? "no-store",
      signal: args.signal,
      headers: {
        Accept: "application/json",
      },
    });

    if (!res.ok) {
      let bodyText: string | undefined;
      try {
        bodyText = await res.text();
      } catch {
        // ignore
      }
      throw new CongressGovError(`Congress.gov request failed (${res.status})`, {
        status: res.status,
        url,
        bodyText,
      });
    }

    return (await res.json()) as T;
  }

  return { fetchJson };
}

