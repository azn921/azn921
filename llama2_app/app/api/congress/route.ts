import { NextRequest, NextResponse } from "next/server";
import { CongressGovError, createCongressGovClient } from "@/lib/congressgov";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function collectQueryParams(searchParams: URLSearchParams): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};

  searchParams.forEach((v, k) => {
    if (k === "endpoint") return;
    const existing = out[k];
    if (existing === undefined) {
      out[k] = v;
      return;
    }
    if (Array.isArray(existing)) {
      existing.push(v);
      return;
    }
    out[k] = [existing, v];
  });

  return out;
}

/**
 * Proxy to the official Congress.gov v3 API.
 *
 * Usage:
 * - GET /api/congress?endpoint=/bill&limit=20&offset=0
 * - GET /api/congress?endpoint=/bill/118/hr/1
 * - GET /api/congress?endpoint=/member&limit=25
 *
 * The server injects `api_key` from `CONGRESS_GOV_API_KEY`.
 */
export async function GET(request: NextRequest) {
  const endpoint = request.nextUrl.searchParams.get("endpoint") ?? "/bill";

  try {
    const client = createCongressGovClient();
    const data = await client.fetchJson({
      endpoint,
      query: collectQueryParams(request.nextUrl.searchParams),
    });
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof CongressGovError) {
      return NextResponse.json(
        {
          error: err.message,
          upstreamStatus: err.status,
          url: err.url,
          upstreamBody: err.bodyText,
        },
        { status: 502 },
      );
    }

    return NextResponse.json(
      {
        error: err instanceof Error ? err.message : "Unknown error",
      },
      { status: 500 },
    );
  }
}

