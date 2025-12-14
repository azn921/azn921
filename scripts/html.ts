import { decodeHtmlEntities } from "./url.js";

export type HtmlLink = {
  href: string;
  text: string;
};

// Lightweight anchor extractor sufficient for senate.gov/house.gov committee lists.
// Not a full HTML parser; deliberately conservative.
export function extractAnchorLinks(html: string): HtmlLink[] {
  const out: HtmlLink[] = [];

  // Capture <a ... href="..."> ... </a>
  const re = /<a\b[^>]*?\bhref\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))[^>]*>([\s\S]*?)<\/a>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) !== null) {
    const href = (m[1] ?? m[2] ?? m[3] ?? "").trim();
    if (!href) continue;

    // Strip nested tags from link text
    const rawText = (m[4] ?? "")
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ");
    const text = decodeHtmlEntities(rawText).replace(/\s+/g, " ").trim();

    out.push({ href, text });
  }

  return out;
}

