import { readFile, writeFile } from "node:fs/promises";
import { fetchFollow } from "./http.js";
import { extractAnchorLinks } from "./html.js";
import { decodeHtmlEntities, normalizeUrl } from "./url.js";
import type { CommitteeWebsite } from "./discoverCommitteeWebsites.js";

type RobotsInfo = {
  found: boolean;
  blockedAll: boolean;
  sitemaps: string[];
};

type SiteCrawlMap = {
  chamber: "senate" | "house";
  name: string;
  siteRoot: string;
  finalSiteRoot: string;
  robots: RobotsInfo;
  discoveredSitemaps: string[];
  pagesFromSitemapsCount: number;
  pagesFromSitemapsSample: string[];
  pagesFromHomepageCount: number;
  pagesFromHomepageSample: string[];
  notes: string[];
};

async function fetchText(url: string): Promise<{ ok: boolean; status: number; text: string; finalUrl: string }> {
  const { response, finalUrl } = await fetchFollow(url, {
    headers: {
      "user-agent": "committee-crawl-prep/1.0 (+https://example.invalid)",
      accept: "text/html,application/xml,text/plain,*/*",
    },
    maxRedirects: 10,
  });
  const text = await response.text().catch(() => "");
  return { ok: response.ok, status: response.status, text, finalUrl };
}

function parseRobots(robotsTxt: string): RobotsInfo {
  const lines = robotsTxt
    .split(/\r?\n/)
    .map((l) => l.replace(/#.*$/, "").trim())
    .filter(Boolean);

  const sitemaps: string[] = [];

  // Very small robots parser focusing on:
  // - Sitemap: directives
  // - User-agent: * blocks (Disallow: /)
  let currentAgents: string[] = [];
  const disallowByAgent = new Map<string, string[]>();

  for (const line of lines) {
    const m = /^([A-Za-z-]+)\s*:\s*(.*)$/.exec(line);
    if (!m) continue;
    const key = m[1]!.toLowerCase();
    const value = m[2]!.trim();

    if (key === "sitemap") {
      const v = value;
      if (v) sitemaps.push(v);
      continue;
    }

    if (key === "user-agent") {
      currentAgents = value ? [value.toLowerCase()] : [];
      continue;
    }

    if (key === "disallow") {
      for (const a of currentAgents) {
        const arr = disallowByAgent.get(a) ?? [];
        arr.push(value);
        disallowByAgent.set(a, arr);
      }
      continue;
    }
  }

  const starDisallows = disallowByAgent.get("*") ?? [];
  const blockedAll = starDisallows.some((d) => d.trim() === "/");
  return { found: true, blockedAll, sitemaps: Array.from(new Set(sitemaps)) };
}

function extractSitemapLocs(xml: string): string[] {
  const out: string[] = [];
  const re = /<loc>\s*([^<]+?)\s*<\/loc>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(xml)) !== null) {
    const loc = decodeHtmlEntities(m[1] ?? "").trim();
    if (!loc) continue;
    out.push(loc);
  }
  return out;
}

async function expandSitemaps(initial: string[], limits: { maxDepth: number; maxSitemaps: number; maxUrls: number }) {
  const seenSitemaps = new Set<string>();
  const pages: string[] = [];

  async function walk(sitemapUrl: string, depth: number): Promise<void> {
    if (depth > limits.maxDepth) return;
    if (seenSitemaps.size >= limits.maxSitemaps) return;
    if (seenSitemaps.has(sitemapUrl)) return;
    seenSitemaps.add(sitemapUrl);

    const { ok, text } = await fetchText(sitemapUrl);
    if (!ok || !text) return;

    const locs = extractSitemapLocs(text);
    if (/<sitemapindex\b/i.test(text)) {
      for (const loc of locs) {
        if (seenSitemaps.size >= limits.maxSitemaps) break;
        await walk(loc, depth + 1);
      }
      return;
    }

    // urlset
    for (const loc of locs) {
      if (pages.length >= limits.maxUrls) break;
      pages.push(loc);
    }
  }

  for (const s of initial) {
    if (seenSitemaps.size >= limits.maxSitemaps) break;
    await walk(s, 0);
  }

  return { sitemaps: Array.from(seenSitemaps), pages };
}

function isSameSite(siteRoot: string, url: string): boolean {
  try {
    const a = new URL(siteRoot);
    const b = new URL(url);
    return a.protocol === b.protocol && a.host === b.host;
  } catch {
    return false;
  }
}

async function homepageLinkDiscovery(siteRoot: string, maxLinks: number): Promise<string[]> {
  const { ok, text } = await fetchText(siteRoot);
  if (!ok || !text) return [];
  const links = extractAnchorLinks(text);

  const out: string[] = [];
  const seen = new Set<string>();
  for (const l of links) {
    let abs: string;
    try {
      abs = new URL(l.href, siteRoot).toString();
    } catch {
      continue;
    }
    const n = normalizeUrl(abs);
    if (!n.normalized) continue;
    if (!isSameSite(siteRoot, n.normalized)) continue;
    if (seen.has(n.normalized)) continue;
    seen.add(n.normalized);
    out.push(n.normalized);
    if (out.length >= maxLinks) break;
  }
  return out;
}

async function main(): Promise<void> {
  const raw = await readFile("/workspace/data/committee_websites.json", "utf8");
  const parsed = JSON.parse(raw) as { committees: CommitteeWebsite[] };
  const committees = parsed.committees ?? [];

  const maps: SiteCrawlMap[] = [];

  for (const c of committees) {
    const finalSiteRoot = c.finalSiteRoot ?? c.siteRoot;
    const notes: string[] = [];

    // robots.txt
    const robotsUrl = new URL("/robots.txt", finalSiteRoot).toString();
    const robotsFetch = await fetchText(robotsUrl);
    let robots: RobotsInfo;
    if (!robotsFetch.ok) {
      robots = { found: false, blockedAll: false, sitemaps: [] };
      notes.push(`robots.txt not found (${robotsFetch.status})`);
    } else {
      robots = parseRobots(robotsFetch.text);
    }

    if (robots.blockedAll) {
      maps.push({
        chamber: c.chamber,
        name: c.name,
        siteRoot: c.siteRoot,
        finalSiteRoot,
        robots,
        discoveredSitemaps: robots.sitemaps,
        pagesFromSitemapsCount: 0,
        pagesFromSitemapsSample: [],
        pagesFromHomepageCount: 0,
        pagesFromHomepageSample: [],
        notes: [...notes, "robots.txt appears to disallow all crawling for User-agent: *"],
      });
      continue;
    }

    // Sitemaps: from robots + common defaults
    const sitemapCandidates = [
      ...robots.sitemaps,
      new URL("/sitemap.xml", finalSiteRoot).toString(),
      new URL("/sitemap_index.xml", finalSiteRoot).toString(),
    ];
    const uniqueCandidates = Array.from(new Set(sitemapCandidates));

    const expanded = await expandSitemaps(uniqueCandidates, { maxDepth: 2, maxSitemaps: 50, maxUrls: 5000 });
    const sitePages = expanded.pages.filter((u) => isSameSite(finalSiteRoot, u));

    let homepageLinks: string[] = [];
    if (sitePages.length === 0) {
      homepageLinks = await homepageLinkDiscovery(finalSiteRoot, 250);
      if (homepageLinks.length === 0) notes.push("No sitemap URLs found; homepage link discovery returned 0 links");
    }

    maps.push({
      chamber: c.chamber,
      name: c.name,
      siteRoot: c.siteRoot,
      finalSiteRoot,
      robots,
      discoveredSitemaps: expanded.sitemaps,
      pagesFromSitemapsCount: sitePages.length,
      pagesFromSitemapsSample: sitePages.slice(0, 50),
      pagesFromHomepageCount: homepageLinks.length,
      pagesFromHomepageSample: homepageLinks.slice(0, 50),
      notes,
    });
  }

  const payload = {
    generatedAt: new Date().toISOString(),
    count: maps.length,
    sites: maps,
  };

  await writeFile("/workspace/data/committee_crawl_map.json", JSON.stringify(payload, null, 2), "utf8");
  console.log(`Wrote /workspace/data/committee_crawl_map.json (${maps.length} sites)`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});

