#!/usr/bin/env python3
"""Congress.gov legislative data extractor + normalizer.

- Uses ONLY official Congress.gov API endpoints under https://api.congress.gov/v3
- Stores raw + normalized outputs under ./data

Env:
  CONGRESS_API_KEY (required)

Examples:
  python congress_gov_service.py --bill HR1234 --congress 119
  python congress_gov_service.py --search "NDAA" --congress 119
  python congress_gov_service.py --committee "House Armed Services" --json-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import requests
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency 'requests'. Install with: pip install -r requirements.txt"
    ) from e


LOG = logging.getLogger("congress_gov")


API_BASE_URL = "https://api.congress.gov/v3"


BILL_TYPE_ALIASES: Dict[str, str] = {
    "hr": "hr",
    "h.r": "hr",
    "house bill": "hr",
    "s": "s",
    "s.": "s",
    "senate bill": "s",
    "hjres": "hjres",
    "h.j.res": "hjres",
    "sjres": "sjres",
    "s.j.res": "sjres",
    "hconres": "hconres",
    "h.con.res": "hconres",
    "sconres": "sconres",
    "s.con.res": "sconres",
    "hres": "hres",
    "h.res": "hres",
    "sres": "sres",
    "s.res": "sres",
}


WEIGHTED_COMMITTEES: List[Tuple[re.Pattern, int]] = [
    (re.compile(r"\barmed services\b", re.I), 30),
    (re.compile(r"\bappropriations\b", re.I), 30),
    (re.compile(r"\benergy\b.*\bcommerce\b|\benergy and commerce\b", re.I), 25),
    (re.compile(r"\bways and means\b", re.I), 25),
    (re.compile(r"\bfinance\b", re.I), 20),
    (re.compile(r"\bforeign relations\b", re.I), 18),
    (re.compile(r"\bjudiciary\b", re.I), 18),
    (re.compile(r"\bhomeland security\b", re.I), 18),
    (re.compile(r"\bhealth\b.*\b(labor|education)\b|\bhelp\b", re.I), 16),
    (re.compile(r"\btransportation\b|\binfrastructure\b", re.I), 14),
]


FLOOR_SIGNAL_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bplaced on (the )?calendar\b", re.I),
    re.compile(r"\bpassed (the )?house\b", re.I),
    re.compile(r"\bpassed (the )?senate\b", re.I),
    re.compile(r"\breceived in the senate\b|\breceived in the house\b", re.I),
    re.compile(r"\bconsidered\b.*\brules\b|\brule (xiii|xiv|xv)\b", re.I),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_date(d: Optional[str]) -> Optional[datetime]:
    if not d:
        return None
    # Congress.gov often uses YYYY-MM-DD or full ISO.
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.strip()):
            return datetime.strptime(d.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(d.replace("Z", "+00:00"))
    except Exception:
        return None


def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def safe_get(obj: Any, path: List[Any], default: Any = None) -> Any:
    cur = obj
    for p in path:
        try:
            if isinstance(p, int):
                if not isinstance(cur, list) or len(cur) <= p:
                    return default
                cur = cur[p]
            else:
                if not isinstance(cur, dict) or p not in cur:
                    return default
                cur = cur[p]
        except Exception:
            return default
    return cur


def _first_str(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_bill_identifier(text: str) -> Optional[Tuple[str, int]]:
    """Parse inputs like: HR1234, H.R.1234, S. 567, hjres12."""
    if not text or not text.strip():
        return None
    s = re.sub(r"\s+", "", text.strip())
    s = s.replace("-", "")
    s = s.replace("_", "")

    # Match common patterns: HR1234 / H.R.1234 / S567 / S.567 / SJRes12 / HConRes5
    m = re.match(r"^(?P<typ>[A-Za-z\.]+?)(?P<num>\d+)$", s)
    if not m:
        return None

    typ_raw = m.group("typ").lower().strip(".")
    num = int(m.group("num"))

    # Normalize type tokens
    typ_raw = typ_raw.replace("..", ".")
    typ_raw = typ_raw.replace("res", "res")

    # Try direct
    if typ_raw in BILL_TYPE_ALIASES:
        return (BILL_TYPE_ALIASES[typ_raw], num)

    # Try dotted formatting variants
    typ_dotted = ".".join(list(typ_raw))
    if typ_dotted in BILL_TYPE_ALIASES:
        return (BILL_TYPE_ALIASES[typ_dotted], num)

    # Try canonicalization of known prefixes
    typ_raw = typ_raw.replace("hjr", "hjres").replace("sjr", "sjres")
    typ_raw = typ_raw.replace("hcr", "hconres").replace("scr", "sconres")
    typ_raw = typ_raw.replace("hres", "hres").replace("sres", "sres")
    if typ_raw in BILL_TYPE_ALIASES:
        return (BILL_TYPE_ALIASES[typ_raw], num)

    return None


@dataclass
class DataStore:
    base_dir: str

    def ensure(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.base_dir, name)

    def load_json(self, name: str, default: Any) -> Any:
        p = self._path(name)
        if not os.path.exists(p):
            return default
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def save_json(self, name: str, obj: Any) -> None:
        p = self._path(name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)


class CongressGovClient:
    def __init__(self, api_key: str, timeout_s: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "policy-legislative-extractor/1.0 (requests)",
                "Accept": "application/json",
            }
        )

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        url = API_BASE_URL + path
        q: Dict[str, Any] = {"api_key": self.api_key, "format": "json"}
        if params:
            for k, v in params.items():
                if v is None:
                    continue
                q[k] = v
        resp = self.session.get(url, params=q, timeout=self.timeout_s)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} for GET {path}: {resp.text[:500]}")
        try:
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Non-JSON response for GET {path}: {resp.text[:200]}") from e

    def paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 250,
        max_pages: int = 200,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        pages = 0
        while pages < max_pages:
            pages += 1
            p = dict(params or {})
            p.setdefault("limit", limit)
            p.setdefault("offset", offset)
            data = self.get(path, p)

            page_items = extract_items_from_response(data)
            if not page_items:
                break
            items.extend(page_items)

            pagination = data.get("pagination") if isinstance(data, dict) else None
            if isinstance(pagination, dict):
                count = pagination.get("count")
                if isinstance(count, int) and count <= len(items):
                    break
            # Fallback: stop if fewer than limit returned
            if len(page_items) < int(p.get("limit") or limit):
                break
            offset += int(p.get("limit") or limit)
            time.sleep(0.1)
        return items


def extract_items_from_response(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Congress.gov v3 responses vary: try to find the primary list container."""
    if not isinstance(data, dict):
        return []

    # Common top-level collection keys
    for key in (
        "bills",
        "billSummaries",
        "summaries",
        "actions",
        "titles",
        "subjects",
        "committees",
        "members",
        "cosponsors",
        "relatedBills",
        "congressionalRecord",
        "committeeReports",
    ):
        v = data.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]

    # Some endpoints wrap list under a singular key
    for key, v in data.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v

    return []


def pick_title(titles: List[Dict[str, Any]], kind: str) -> str:
    """kind: 'short' or 'official'"""
    if not titles:
        return ""

    def _title_text(t: Dict[str, Any]) -> str:
        return _first_str(t.get("title"), t.get("text"), t.get("titleText"))

    def _type(t: Dict[str, Any]) -> str:
        return _first_str(t.get("titleType"), t.get("type"), t.get("titleTypeCode"))

    wanted = "short" if kind == "short" else "official"

    # Prefer explicit matches
    for t in titles:
        tt = _type(t).lower()
        if wanted in tt:
            txt = _title_text(t)
            if txt:
                return txt

    # Secondary preference ordering
    if kind == "official":
        for t in titles:
            tt = _type(t).lower()
            if "introduced" in tt and ("title" in tt or "official" in tt):
                txt = _title_text(t)
                if txt:
                    return txt

    # Fallback to first title
    for t in titles:
        txt = _title_text(t)
        if txt:
            return txt

    return ""


def pick_latest_summary(summaries: List[Dict[str, Any]]) -> str:
    if not summaries:
        return ""

    def _date_key(s: Dict[str, Any]) -> datetime:
        d = _first_str(s.get("updateDate"), s.get("actionDate"), s.get("date"))
        dt = parse_iso_date(d)
        return dt or datetime(1970, 1, 1, tzinfo=timezone.utc)

    summaries_sorted = sorted(summaries, key=_date_key, reverse=True)
    for s in summaries_sorted:
        txt = _first_str(s.get("text"), s.get("summary"), s.get("description"))
        if txt:
            return txt
    return ""


def pick_latest_action(raw_bill: Dict[str, Any], actions: List[Dict[str, Any]]) -> Dict[str, str]:
    la = raw_bill.get("latestAction") if isinstance(raw_bill, dict) else None
    if isinstance(la, dict):
        d = _first_str(la.get("actionDate"), la.get("date"))
        t = _first_str(la.get("text"), la.get("actionText"))
        if d or t:
            return {"date": d, "text": t}

    if actions:
        def _action_date(a: Dict[str, Any]) -> datetime:
            d = _first_str(a.get("actionDate"), a.get("date"))
            dt = parse_iso_date(d)
            return dt or datetime(1970, 1, 1, tzinfo=timezone.utc)

        a0 = sorted(actions, key=_action_date, reverse=True)[0]
        return {
            "date": _first_str(a0.get("actionDate"), a0.get("date")),
            "text": _first_str(a0.get("text"), a0.get("actionText"), a0.get("description")),
        }

    return {"date": "", "text": ""}


def derive_status_stage(
    raw_bill: Dict[str, Any],
    actions: List[Dict[str, Any]],
    committees: List[Dict[str, Any]],
    latest_action_text: str,
) -> str:
    text_blob = " ".join(
        [
            latest_action_text or "",
            json.dumps(actions[:25], ensure_ascii=False) if actions else "",
        ]
    ).lower()

    if re.search(r"\b(public|private) law\b|\bbecame law\b|\bsigned by president\b", text_blob):
        return "enacted"
    if re.search(r"\bconference\b|\bconferees\b|\bappoint\b.*\bconferee\b", text_blob):
        return "conference"

    passed_house = bool(re.search(r"\bpassed (the )?house\b|\bagreed to in (the )?house\b", text_blob))
    passed_senate = bool(re.search(r"\bpassed (the )?senate\b|\bagreed to in (the )?senate\b", text_blob))
    if passed_house and not passed_senate:
        return "passed_house"
    if passed_senate and not passed_house:
        return "passed_senate"
    if passed_house and passed_senate:
        # still map to a single stage; conference/enacted would have been caught above
        return "passed_senate"

    if re.search(r"\breported\b|\bordered to be reported\b|\breport to accompany\b", text_blob):
        return "reported"

    if committees:
        return "committee"

    return "introduced"


def bill_flags(title_official: str, title_short: str, summary: str) -> Dict[str, bool]:
    blob = f"{title_official} {title_short} {summary}".lower()
    is_ndaa = bool(re.search(r"\bndaa\b|\bnational defense authorization act\b", blob))
    is_appropriations = bool(
        re.search(
            r"\bappropriation(s)?\b|\bdepartment of defense appropriations\b|\bcontinuing resolution\b|\bcr\b",
            blob,
        )
    )
    return {"ndaa": is_ndaa, "appropriations": is_appropriations}


def compute_urgency_score(
    latest_action_date: str,
    latest_action_text: str,
    flags: Dict[str, bool],
    status_stage: str,
) -> int:
    dt = parse_iso_date(latest_action_date)
    days_since = 999
    if dt:
        days_since = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 86400))

    base = 100 - (days_since * 3)
    score = base

    if any(p.search(latest_action_text or "") for p in FLOOR_SIGNAL_PATTERNS):
        score += 15

    if flags.get("ndaa"):
        score += 10
    if flags.get("appropriations"):
        score += 12

    if status_stage == "conference":
        score += 10
    if status_stage in ("passed_house", "passed_senate"):
        score += 8
    if dt and days_since <= 7:
        score += 8

    return int(clamp(score, 0, 100))


def compute_influence_points(
    committees_norm: List[Dict[str, Any]],
    sponsor_party: str,
    cosponsors_norm: List[Dict[str, Any]],
    sponsor_terms_count: Optional[int],
    flags: Dict[str, bool],
) -> int:
    pts = 0

    # Committee weighting
    committee_names = [
        _first_str(c.get("committee_name"), c.get("name"), "") for c in committees_norm
    ]
    weights = []
    for name in committee_names:
        for pat, w in WEIGHTED_COMMITTEES:
            if name and pat.search(name):
                weights.append(w)
    if weights:
        pts += max(weights)
        pts += min(10, (len(weights) - 1) * 3)

    # Sponsor seniority
    if sponsor_terms_count is not None:
        pts += int(clamp(sponsor_terms_count * 3, 0, 20))

    # Bipartisan cosponsorship
    parties = set()
    if sponsor_party:
        parties.add(sponsor_party.strip())
    for c in cosponsors_norm:
        p = _first_str(c.get("party"))
        if p:
            parties.add(p)
    if len(parties) >= 2:
        pts += 12
        # stronger if many opposite-party cosponsors
        if sponsor_party:
            opp = sum(1 for c in cosponsors_norm if _first_str(c.get("party")) and _first_str(c.get("party")) != sponsor_party)
            if cosponsors_norm:
                ratio = opp / max(1, len(cosponsors_norm))
                if ratio >= 0.33:
                    pts += 6

    if flags.get("ndaa"):
        pts += 8
    if flags.get("appropriations"):
        pts += 10

    return int(clamp(pts, 0, 1000))


def recommend_ask_type(
    status_stage: str,
    flags: Dict[str, bool],
    title_official: str,
    title_short: str,
    summary: str,
    latest_action_text: str,
) -> str:
    blob = f"{title_official} {title_short} {summary} {latest_action_text}".lower()

    if flags.get("appropriations"):
        return "appropriations_insert"
    if re.search(r"\boversight\b|\binvestigation\b|\binspector general\b", blob):
        return "oversight_letter"
    if re.search(r"\bpilot program\b|\bpilot\b", blob):
        return "pilot_program"

    if status_stage == "introduced":
        return "cosponsorship"
    if status_stage == "committee":
        return "report_language"
    if status_stage == "reported":
        return "amendment"
    if status_stage in ("passed_house", "passed_senate", "conference"):
        return "amendment"

    return "report_language"


def normalize_committees(committees_raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in committees_raw or []:
        chamber = _first_str(c.get("chamber"), c.get("chamberName"))
        name = _first_str(c.get("name"), c.get("committeeName"))
        code = _first_str(c.get("systemCode"), c.get("committeeCode"), c.get("code"))
        sub = ""
        sc = c.get("subcommittee")
        if isinstance(sc, dict):
            sub = _first_str(sc.get("name"), sc.get("subcommitteeName"))
        elif isinstance(c.get("subcommittee"), str):
            sub = _first_str(c.get("subcommittee"))

        if chamber and chamber.lower().startswith("h"):
            chamber = "House"
        elif chamber and chamber.lower().startswith("s"):
            chamber = "Senate"

        out.append(
            {
                "chamber": chamber or "",
                "committee_name": name,
                "committee_code": code,
                "subcommittee": sub,
            }
        )
    return out


def normalize_cosponsors(cosponsors_raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for c in cosponsors_raw or []:
        out.append(
            {
                "name": _first_str(c.get("fullName"), c.get("name")),
                "party": _first_str(c.get("party")),
                "state": _first_str(c.get("state")),
            }
        )
    # Dedup by name+party+state
    seen = set()
    deduped = []
    for c in out:
        k = (c.get("name"), c.get("party"), c.get("state"))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)
    return deduped


def normalize_subjects(subjects_raw: List[Dict[str, Any]]) -> List[str]:
    subjects: List[str] = []
    for s in subjects_raw or []:
        nm = _first_str(s.get("name"), s.get("subject"), s.get("term"))
        if nm:
            subjects.append(nm)
    # Dedup preserve order
    seen = set()
    out = []
    for nm in subjects:
        if nm in seen:
            continue
        seen.add(nm)
        out.append(nm)
    return out


def normalize_bill(
    raw_bill: Dict[str, Any],
    summaries: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    titles: List[Dict[str, Any]],
    subjects: List[Dict[str, Any]],
    committees: List[Dict[str, Any]],
    cosponsors: List[Dict[str, Any]],
    related_bills: List[Dict[str, Any]],
    congress: int,
    bill_type: str,
    bill_number: int,
    sponsor_terms_count: Optional[int],
) -> Dict[str, Any]:
    title_short = pick_title(titles, "short")
    title_official = pick_title(titles, "official")
    if not title_official:
        title_official = _first_str(raw_bill.get("title"), raw_bill.get("titleText"))

    summary = pick_latest_summary(summaries)

    latest_action = pick_latest_action(raw_bill, actions)
    committees_norm = normalize_committees(committees)
    cosponsors_norm = normalize_cosponsors(cosponsors)
    subjects_norm = normalize_subjects(subjects)

    policy_topics: List[str] = []
    policy_area = raw_bill.get("policyArea")
    if isinstance(policy_area, dict):
        nm = _first_str(policy_area.get("name"))
        if nm:
            policy_topics.append(nm)
    elif isinstance(policy_area, str) and policy_area.strip():
        policy_topics.append(policy_area.strip())

    sponsor0 = safe_get(raw_bill, ["sponsors", 0], {})
    sponsor = {
        "name": _first_str(sponsor0.get("fullName"), sponsor0.get("name")),
        "party": _first_str(sponsor0.get("party")),
        "state": _first_str(sponsor0.get("state")),
        "bioguide_id": _first_str(sponsor0.get("bioguideId"), sponsor0.get("bioguideID")),
    }

    status_stage = derive_status_stage(raw_bill, actions, committees, latest_action.get("text", ""))
    flags = bill_flags(title_official, title_short, summary)

    urgency_score = compute_urgency_score(
        latest_action.get("date", ""),
        latest_action.get("text", ""),
        flags,
        status_stage,
    )

    influence_points = compute_influence_points(
        committees_norm,
        sponsor.get("party", ""),
        cosponsors_norm,
        sponsor_terms_count,
        flags,
    )

    recommended = recommend_ask_type(
        status_stage,
        flags,
        title_official,
        title_short,
        summary,
        latest_action.get("text", ""),
    )

    bill_id = f"{bill_type.upper()}{bill_number}-{congress}"
    last_updated = _first_str(raw_bill.get("updateDate"), raw_bill.get("updateDateIncludingText"))
    if not last_updated:
        last_updated = utc_now_iso()

    # related_bills not required in schema; keep only for raw storage.
    _ = related_bills

    return {
        "bill_id": bill_id,
        "bill_type": bill_type,
        "number": bill_number,
        "congress": congress,
        "title_short": title_short,
        "title_official": title_official,
        "summary": summary,
        "latest_action": {
            "date": latest_action.get("date", ""),
            "text": latest_action.get("text", ""),
        },
        "status_stage": status_stage,
        "policy_topics": policy_topics,
        "subjects": subjects_norm,
        "sponsor": sponsor,
        "cosponsors": cosponsors_norm,
        "committees": committees_norm,
        "last_updated": last_updated,
        # Lobbying metadata
        "urgency_score": urgency_score,
        "influence_points": influence_points,
        "recommended_ask_type": recommended,
    }


def fetch_bill_full(
    client: CongressGovClient,
    congress: int,
    bill_type: str,
    bill_number: int,
    fetch_related: bool = True,
) -> Dict[str, Any]:
    detail = client.get(f"/bill/{congress}/{bill_type}/{bill_number}")

    # Bill detail may come under 'bill' key.
    raw_bill = detail.get("bill") if isinstance(detail, dict) else None
    if not isinstance(raw_bill, dict):
        raw_bill = detail

    # Sub-resources (graceful if missing)
    summaries = _safe_sublist(client, f"/bill/{congress}/{bill_type}/{bill_number}/summaries")
    actions = _safe_sublist(client, f"/bill/{congress}/{bill_type}/{bill_number}/actions")
    titles = _safe_sublist(client, f"/bill/{congress}/{bill_type}/{bill_number}/titles")
    subjects = _safe_sublist(client, f"/bill/{congress}/{bill_type}/{bill_number}/subjects")
    committees = _safe_sublist(client, f"/bill/{congress}/{bill_type}/{bill_number}/committees")
    cosponsors = _safe_sublist(client, f"/bill/{congress}/{bill_type}/{bill_number}/cosponsors")

    related_bills: List[Dict[str, Any]] = []
    if fetch_related:
        related_bills = _safe_sublist(client, f"/bill/{congress}/{bill_type}/{bill_number}/relatedbills")

    return {
        "bill": raw_bill,
        "summaries": summaries,
        "actions": actions,
        "titles": titles,
        "subjects": subjects,
        "committees": committees,
        "cosponsors": cosponsors,
        "relatedbills": related_bills,
    }


def _safe_sublist(client: CongressGovClient, path: str) -> List[Dict[str, Any]]:
    try:
        data = client.get(path)
        return extract_items_from_response(data)
    except Exception as e:
        LOG.debug("Subresource unavailable %s (%s)", path, e)
        return []


def discover_bills_by_search(
    client: CongressGovClient,
    congress: Optional[int],
    query: str,
    limit: int = 50,
) -> List[Tuple[int, str, int]]:
    # Use /bill/{congress} when provided; otherwise /bill
    path = f"/bill/{congress}" if congress else "/bill"
    params = {"query": query, "limit": min(limit, 250), "offset": 0}

    bills = client.paginate(path, params=params, limit=min(limit, 250), max_pages=10)

    out: List[Tuple[int, str, int]] = []
    for b in bills:
        c = safe_get(b, ["congress"], congress)
        bt = _first_str(b.get("type"), b.get("billType"), b.get("billTypeCode"))
        num = b.get("number")
        if isinstance(num, str) and num.isdigit():
            num = int(num)
        if not isinstance(num, int):
            # sometimes nested
            num2 = safe_get(b, ["billNumber"], None)
            if isinstance(num2, int):
                num = num2
            elif isinstance(num2, str) and num2.isdigit():
                num = int(num2)

        if isinstance(c, str) and c.isdigit():
            c = int(c)
        if not isinstance(c, int) or not bt or not isinstance(num, int):
            continue

        bt = bt.lower().strip()
        out.append((c, bt, num))

    # Dedup
    seen = set()
    deduped = []
    for c, bt, num in out:
        k = (c, bt, num)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(k)
    return deduped


def fetch_committees_filtered(
    client: CongressGovClient,
    chamber: Optional[str],
    name_filters: List[str],
    limit: int = 500,
) -> List[Dict[str, Any]]:
    path = "/committee"
    if chamber:
        ch = chamber.strip().lower()
        if ch in ("house", "h"):
            path = "/committee/house"
        elif ch in ("senate", "s"):
            path = "/committee/senate"
    committees = client.paginate(path, params={"limit": min(limit, 250)}, limit=min(limit, 250), max_pages=10)

    if not name_filters:
        return committees

    needles = [nf.lower().strip() for nf in name_filters if nf and nf.strip()]
    out = []
    for c in committees:
        nm = _first_str(c.get("name"), c.get("committeeName")).lower()
        if any(n in nm for n in needles):
            out.append(c)
    return out


def fetch_members_by_name(
    client: CongressGovClient,
    name: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    # Official endpoint: /member (supports query params; name commonly supported)
    members = client.paginate("/member", params={"name": name, "limit": min(limit, 250)}, limit=min(limit, 250), max_pages=5)
    return members


def fetch_member_detail_terms_count(client: CongressGovClient, bioguide_id: str) -> Optional[int]:
    if not bioguide_id:
        return None
    try:
        data = client.get(f"/member/{bioguide_id}")
    except Exception:
        return None

    m = data.get("member") if isinstance(data, dict) else None
    if not isinstance(m, dict):
        m = data if isinstance(data, dict) else {}

    terms = m.get("terms")
    if isinstance(terms, dict):
        # sometimes terms: { item: [...] }
        items = terms.get("item")
        if isinstance(items, list):
            return len(items)
    if isinstance(terms, list):
        return len(terms)

    # Fallback: count across "terms" keys nested
    items2 = safe_get(m, ["terms", "item"], None)
    if isinstance(items2, list):
        return len(items2)

    return None


def merge_bills_by_id(existing: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for b in existing or []:
        bid = _first_str(b.get("bill_id"))
        if bid:
            by_id[bid] = b
    for b in new or []:
        bid = _first_str(b.get("bill_id"))
        if not bid:
            continue
        by_id[bid] = b
    # Stable-ish ordering: sort by congress desc, then type, then number
    def _key(b: Dict[str, Any]) -> Tuple[int, str, int]:
        return (
            int(b.get("congress") or 0),
            _first_str(b.get("bill_type")),
            int(b.get("number") or 0),
        )

    merged = list(by_id.values())
    merged.sort(key=_key, reverse=True)
    return merged


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract, normalize, and store Congress.gov legislative data")
    parser.add_argument("--congress", type=int, default=119, help="Congress number (default: 119)")
    parser.add_argument("--bill", action="append", default=[], help="Specific bill id (e.g., HR1234, S.567). Can repeat.")
    parser.add_argument("--search", type=str, default="", help="Keyword search for discovery (e.g., NDAA, appropriations)")
    parser.add_argument("--committee", action="append", default=[], help="Committee name filter (can repeat)")
    parser.add_argument("--committee-chamber", type=str, default="", help="Optional: house|senate for committee listing")
    parser.add_argument("--member", action="append", default=[], help="Member name filter (can repeat; uses /member)")
    parser.add_argument("--json-only", action="store_true", help="Skip member enrichment for faster export")
    parser.add_argument("--max-bills", type=int, default=50, help="Max discovered bills from search (default: 50)")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level (DEBUG, INFO, WARNING)")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    api_key = os.getenv("CONGRESS_API_KEY", "").strip()
    if not api_key:
        LOG.error("CONGRESS_API_KEY is not set")
        return 2

    store = DataStore(base_dir=os.path.join(os.getcwd(), "data"))
    store.ensure()

    client = CongressGovClient(api_key=api_key)

    # Load existing outputs for dedupe
    existing_norm = store.load_json("bills_normalized.json", default=[])
    existing_raw = store.load_json("bills_raw.json", default=[])
    existing_committees = store.load_json("committees.json", default=[])
    existing_members = store.load_json("members.json", default=[])

    # Resolve bill targets
    bill_targets: List[Tuple[int, str, int]] = []

    # Specific bills
    for btxt in args.bill:
        parsed = parse_bill_identifier(btxt)
        if parsed:
            bt, num = parsed
            bill_targets.append((args.congress, bt, num))
        else:
            # treat as search term
            if not args.search:
                args.search = btxt

    # Keyword discovery
    if args.search and args.search.strip():
        LOG.info("Discovering bills via search: %s", args.search.strip())
        discovered = discover_bills_by_search(client, args.congress, args.search.strip(), limit=max(1, args.max_bills))
        LOG.info("Discovered %d bill(s)", len(discovered))
        bill_targets.extend(discovered)

    # Dedup bill targets
    seen_targets = set()
    dedup_targets = []
    for c, bt, num in bill_targets:
        k = (c, bt, num)
        if k in seen_targets:
            continue
        seen_targets.add(k)
        dedup_targets.append(k)
    bill_targets = dedup_targets

    # Committees (optional)
    committees_out: List[Dict[str, Any]] = []
    if args.committee or args.committee_chamber:
        LOG.info("Fetching committees (filter: %s)", ", ".join(args.committee) if args.committee else "<none>")
        committees_out = fetch_committees_filtered(
            client,
            chamber=args.committee_chamber or None,
            name_filters=args.committee,
        )

    # Members (optional)
    members_out: List[Dict[str, Any]] = []
    if args.member:
        for nm in args.member:
            LOG.info("Fetching members by name: %s", nm)
            members_out.extend(fetch_members_by_name(client, nm))

    # Member enrichment cache
    sponsor_terms_cache: Dict[str, Optional[int]] = {}

    bills_raw_new: List[Dict[str, Any]] = []
    bills_norm_new: List[Dict[str, Any]] = []

    if bill_targets:
        LOG.info("Fetching %d bill(s)", len(bill_targets))
        for idx, (cong, bt, num) in enumerate(bill_targets, start=1):
            LOG.info("[%d/%d] Bill %s%d-%d", idx, len(bill_targets), bt.upper(), num, cong)
            try:
                full = fetch_bill_full(client, cong, bt, num, fetch_related=True)
            except Exception as e:
                LOG.warning("Failed to fetch %s%d-%d: %s", bt.upper(), num, cong, e)
                continue

            raw_bill = full.get("bill") if isinstance(full, dict) else None
            if not isinstance(raw_bill, dict):
                continue

            sponsor_bio = _first_str(safe_get(raw_bill, ["sponsors", 0, "bioguideId"], ""))
            sponsor_terms = None
            if sponsor_bio and not args.json_only:
                if sponsor_bio not in sponsor_terms_cache:
                    sponsor_terms_cache[sponsor_bio] = fetch_member_detail_terms_count(client, sponsor_bio)
                sponsor_terms = sponsor_terms_cache.get(sponsor_bio)

            normalized = normalize_bill(
                raw_bill=raw_bill,
                summaries=full.get("summaries", []),
                actions=full.get("actions", []),
                titles=full.get("titles", []),
                subjects=full.get("subjects", []),
                committees=full.get("committees", []),
                cosponsors=full.get("cosponsors", []),
                related_bills=full.get("relatedbills", []),
                congress=cong,
                bill_type=bt,
                bill_number=num,
                sponsor_terms_count=sponsor_terms,
            )

            bills_raw_new.append(
                {
                    "bill_id": normalized.get("bill_id"),
                    "congress": cong,
                    "bill_type": bt,
                    "number": num,
                    "fetched_at": utc_now_iso(),
                    "raw": full,
                }
            )
            bills_norm_new.append(normalized)

    else:
        LOG.info("No bills requested/discovered; will only write committees/members if provided")

    # Merge + dedupe across runs
    merged_norm = merge_bills_by_id(existing_norm, bills_norm_new)

    # Merge raw: prefer keeping latest fetched_at per bill_id
    raw_by_id: Dict[str, Dict[str, Any]] = {}
    for r in existing_raw or []:
        bid = _first_str(r.get("bill_id"))
        if bid:
            raw_by_id[bid] = r
    for r in bills_raw_new:
        bid = _first_str(r.get("bill_id"))
        if not bid:
            continue
        raw_by_id[bid] = r
    merged_raw = list(raw_by_id.values())

    # Committees + members merge (dedupe lightly)
    def _dedupe_list(items: List[Dict[str, Any]], key_fn) -> List[Dict[str, Any]]:
        seen = set()
        out = []
        for it in items or []:
            k = key_fn(it)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    merged_committees = _dedupe_list((existing_committees or []) + (committees_out or []), lambda c: _first_str(c.get("systemCode"), c.get("committeeCode"), c.get("code"), c.get("name")))
    merged_members = _dedupe_list((existing_members or []) + (members_out or []), lambda m: _first_str(m.get("bioguideId"), m.get("bioguideID"), m.get("name")))

    store.save_json("bills_raw.json", merged_raw)
    store.save_json("bills_normalized.json", merged_norm)
    store.save_json("committees.json", merged_committees)
    store.save_json("members.json", merged_members)

    LOG.info("Saved outputs to %s", store.base_dir)
    LOG.info("- bills_raw.json: %d", len(merged_raw))
    LOG.info("- bills_normalized.json: %d", len(merged_norm))
    LOG.info("- committees.json: %d", len(merged_committees))
    LOG.info("- members.json: %d", len(merged_members))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
