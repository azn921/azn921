import { writeFile } from "node:fs/promises";
import { fetchFollow } from "./http.js";
import { extractAnchorLinks } from "./html.js";
import { normalizeUrl } from "./url.js";

type Chamber = "senate" | "house";

export type CommitteeWebsite = {
  chamber: Chamber;
  name: string;
  sourceListUrl: string;
  sourceUrl: string;
  sourceText: string;
  normalizedUrl: string;
  siteRoot: string;
  finalUrl?: string;
  finalSiteRoot?: string;
  redirectsFollowed?: number;
};

const SENATE_LIST_URL = "https://www.senate.gov/committees/committees_home.htm";
const HOUSE_LIST_URL = "https://www.house.gov/committees";

function isLikelyCommitteeHost(chamber: Chamber, host: string): boolean {
  const h = host.toLowerCase();
  if (chamber === "senate") {
    if (!h.endsWith(".senate.gov")) return false;
    if (h === "www.senate.gov" || h === "senate.gov") return false;
    return true;
  }
  if (!h.endsWith(".house.gov")) return false;
  if (h === "www.house.gov" || h === "house.gov") return false;
  return true;
}

function cleanCommitteeName(text: string, fallbackHost: string): string {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length >= 6) return t;
  return fallbackHost;
}

async function fetchText(url: string): Promise<string> {
  const { response } = await fetchFollow(url, {
    headers: {
      "user-agent": "committee-site-discovery/1.0 (+https://example.invalid)",
      accept: "text/html,application/xhtml+xml",
    },
  });
  if (!response.ok) throw new Error(`Failed to fetch ${url}: ${response.status} ${response.statusText}`);
  return await response.text();
}

async function mapLimit<T, R>(items: T[], limit: number, fn: (item: T, idx: number) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let nextIndex = 0;

  async function worker(): Promise<void> {
    while (true) {
      const i = nextIndex++;
      if (i >= items.length) return;
      results[i] = await fn(items[i]!, i);
    }
  }

  const workers = new Array(Math.min(limit, items.length)).fill(0).map(() => worker());
  await Promise.all(workers);
  return results;
}

async function discoverFromList(chamber: Chamber, listUrl: string): Promise<CommitteeWebsite[]> {
  const html = await fetchText(listUrl);
  const links = extractAnchorLinks(html);

  const rawCandidates = links
    .map((l) => {
      try {
        const abs = new URL(l.href, listUrl).toString();
        return { href: abs, text: l.text };
      } catch {
        return null;
      }
    })
    .filter((x): x is { href: string; text: string } => x !== null);

  const out: CommitteeWebsite[] = [];
  const seen = new Set<string>(); // siteRoot

  for (const c of rawCandidates) {
    const n = normalizeUrl(c.href);
    if (!n.normalized || !n.siteRoot) continue;

    const u = new URL(n.normalized);
    if (!isLikelyCommitteeHost(chamber, u.hostname)) continue;

    const key = n.siteRoot;
    if (seen.has(key)) continue;
    seen.add(key);

    out.push({
      chamber,
      name: cleanCommitteeName(c.text, u.hostname),
      sourceListUrl: listUrl,
      sourceUrl: c.href,
      sourceText: c.text,
      normalizedUrl: n.normalized,
      siteRoot: n.siteRoot,
    });
  }

  return out;
}

async function resolveFinalHomes(items: CommitteeWebsite[]): Promise<CommitteeWebsite[]> {
  return await mapLimit(items, 6, async (item) => {
    // Prefer a cheap HEAD; fall back to GET for servers that block HEAD.
    try {
      const head = await fetchFollow(item.siteRoot, {
        method: "HEAD",
        headers: { "user-agent": "committee-site-discovery/1.0 (+https://example.invalid)" },
        maxRedirects: 10,
      });
      const n = normalizeUrl(head.finalUrl);
      if (n.normalized && n.siteRoot) {
        return { ...item, finalUrl: n.normalized, finalSiteRoot: n.siteRoot, redirectsFollowed: head.redirectsFollowed };
      }
      return item;
    } catch {
      const get = await fetchFollow(item.siteRoot, {
        method: "GET",
        headers: { "user-agent": "committee-site-discovery/1.0 (+https://example.invalid)" },
        maxRedirects: 10,
      });
      const n = normalizeUrl(get.finalUrl);
      if (n.normalized && n.siteRoot) {
        return { ...item, finalUrl: n.normalized, finalSiteRoot: n.siteRoot, redirectsFollowed: get.redirectsFollowed };
      }
      return item;
    }
  });
}

async function main(): Promise<void> {
  const senate = await discoverFromList("senate", SENATE_LIST_URL);
  const house = await discoverFromList("house", HOUSE_LIST_URL);

  const combined = [...senate, ...house].sort((a, b) =>
    a.chamber === b.chamber ? a.name.localeCompare(b.name) : a.chamber.localeCompare(b.chamber)
  );

  const resolved = await resolveFinalHomes(combined);

  const payload = {
    generatedAt: new Date().toISOString(),
    sources: { senate: SENATE_LIST_URL, house: HOUSE_LIST_URL },
    count: resolved.length,
    committees: resolved,
  };

  await writeFile("/workspace/data/committee_websites.json", JSON.stringify(payload, null, 2), "utf8");

  // Also print a simple list for quick copying
  const byChamber: Record<Chamber, CommitteeWebsite[]> = { senate: [], house: [] };
  for (const c of resolved) byChamber[c.chamber].push(c);

  for (const chamber of ["senate", "house"] as const) {
    console.log(`\n${chamber.toUpperCase()} (${byChamber[chamber].length})`);
    for (const c of byChamber[chamber]) {
      console.log(`${c.name}\t${c.finalSiteRoot ?? c.siteRoot}`);
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});

