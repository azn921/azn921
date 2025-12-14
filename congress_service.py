#!/usr/bin/env python3
"""
Congress.gov legislative data extractor + normalizer (official endpoints only).

Outputs (written to ./data):
  - bills_raw.json
  - bills_normalized.json
  - committees.json
  - members.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


BASE_URL = "https://api.congress.gov/v3"
DEFAULT_LIMIT = 250


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _safe_json_dump(obj: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _parse_iso_date(s: Optional[str]) -> Optional[dt.date]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    # Congress.gov commonly returns "YYYY-MM-DD" or full timestamps.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            if fmt.endswith("%z"):
                return dt.datetime.strptime(s, fmt).date()
            if fmt.endswith("Z"):
                return dt.datetime.strptime(s, fmt).date()
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Try fromisoformat as a last resort.
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def infer_current_congress(today: Optional[dt.date] = None) -> int:
    """
    Approximate current Congress number based on year.
    Congress terms start on Jan 3 of odd-numbered years; each term is 2 years.
    119th Congress began in 2025.
    """
    if today is None:
        today = dt.date.today()
    year = today.year
    # If in an even year, we're still in the Congress that began the previous odd year.
    start_year = year if (year % 2 == 1) else (year - 1)
    # 1st Congress started in 1789 (odd).
    return ((start_year - 1789) // 2) + 1


def normalize_bill_type(raw: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", raw or "").lower()
    aliases = {
        "hres": "hres",
        "sres": "sres",
        "hr": "hr",
        "house": "hr",
        "s": "s",
        "senate": "s",
        "hjres": "hjres",
        "sjres": "sjres",
        "hconres": "hconres",
        "sconres": "sconres",
    }
    return aliases.get(s, s)


def parse_bill_identifier(s: str) -> Tuple[str, int, Optional[int]]:
    """
    Accepts: HR1234, H.R.1234, S.567, hjres12, hr1234-118
    Returns: (billType, billNumber, optional congress)
    """
    if not s or not isinstance(s, str):
        raise ValueError("Empty bill identifier")
    s = s.strip()
    m = re.match(r"^\s*([A-Za-z.\- ]+)\s*([0-9]+)\s*(?:[-/ ]\s*([0-9]{3}))?\s*$", s)
    if not m:
        raise ValueError(f"Unrecognized bill identifier: {s!r}")
    bill_type = normalize_bill_type(m.group(1))
    bill_number = int(m.group(2))
    congress = int(m.group(3)) if m.group(3) else None
    return bill_type, bill_number, congress


def build_bill_id(bill_type: str, number: int, congress: int) -> str:
    return f"{bill_type.upper()}{int(number)}-{int(congress)}"


@dataclass
class ClientConfig:
    api_key: str
    base_url: str = BASE_URL
    timeout_s: float = 30.0
    max_retries: int = 5
    user_agent: str = "congress-legislative-data-service/1.0"


class CongressGovClient:
    def __init__(self, cfg: ClientConfig) -> None:
        self.cfg = cfg
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": cfg.user_agent})

    def _full_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return self.cfg.base_url + path_or_url

    def get_json(self, path_or_url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._full_url(path_or_url)
        qp = dict(params or {})
        # Only add api_key/format when we're not following a server-provided next URL.
        if not (path_or_url.startswith("http://") or path_or_url.startswith("https://")):
            qp.setdefault("api_key", self.cfg.api_key)
            qp.setdefault("format", "json")

        last_err: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.sess.get(url, params=qp, timeout=self.cfg.timeout_s)
                if resp.status_code in (429,) or 500 <= resp.status_code <= 599:
                    raise requests.HTTPError(f"retryable http {resp.status_code}", response=resp)
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                last_err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                # 404 is expected for some optional subresources.
                if status == 404:
                    return {}
                if attempt >= self.cfg.max_retries:
                    raise
                sleep_s = min(30.0, (2 ** (attempt - 1)) + random.random())
                time.sleep(sleep_s)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                if attempt >= self.cfg.max_retries:
                    raise
                sleep_s = min(30.0, (2 ** (attempt - 1)) + random.random())
                time.sleep(sleep_s)
            except ValueError as e:
                # JSON decode issue
                last_err = e
                if attempt >= self.cfg.max_retries:
                    raise
                time.sleep(min(10.0, attempt))
        if last_err:
            raise last_err
        raise RuntimeError("Unexpected request failure")

    @staticmethod
    def _detect_items_key(data: Dict[str, Any]) -> Optional[str]:
        for k, v in data.items():
            if k in ("pagination", "request"):
                continue
            if isinstance(v, list):
                return k
        return None

    def iter_paginated(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        items_key: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
        hard_cap: Optional[int] = None,
    ) -> Iterable[Dict[str, Any]]:
        qp = dict(params or {})
        qp.setdefault("limit", limit)
        seen = 0
        next_url: Optional[str] = path
        next_params: Optional[Dict[str, Any]] = qp
        while next_url:
            data = self.get_json(next_url, next_params)
            if not data:
                break
            key = items_key or self._detect_items_key(data)
            items = data.get(key, []) if key else []
            if isinstance(items, dict):
                # Some endpoints may wrap differently; normalize to list.
                items = [items]
            if not isinstance(items, list):
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                yield item
                seen += 1
                if hard_cap is not None and seen >= hard_cap:
                    return
            next_url = (data.get("pagination") or {}).get("next")
            next_params = None  # server-provided URL already includes pagination args

    # ---- Endpoint helpers (official endpoints only) ----

    def list_bills(self, congress: Optional[int] = None, bill_type: Optional[str] = None, query: Optional[str] = None) -> Iterable[Dict[str, Any]]:
        if congress and bill_type:
            path = f"/bill/{int(congress)}/{bill_type}"
        elif congress:
            path = f"/bill/{int(congress)}"
        else:
            path = "/bill"
        params: Dict[str, Any] = {}
        if query:
            # If unsupported by the API for a given endpoint, we'll just get a 4xx and skip upstream.
            params["query"] = query
        return self.iter_paginated(path, params=params, items_key="bills")

    def get_bill(self, congress: int, bill_type: str, bill_number: int) -> Dict[str, Any]:
        return self.get_json(f"/bill/{int(congress)}/{bill_type}/{int(bill_number)}")

    def get_bill_sub(self, congress: int, bill_type: str, bill_number: int, sub: str) -> Dict[str, Any]:
        return self.get_json(f"/bill/{int(congress)}/{bill_type}/{int(bill_number)}/{sub}")

    def list_committees(self, chamber: Optional[str] = None) -> Iterable[Dict[str, Any]]:
        path = f"/committee/{chamber}" if chamber else "/committee"
        return self.iter_paginated(path, params={}, items_key="committees")

    def get_committee(self, chamber: str, committee_code: str) -> Dict[str, Any]:
        return self.get_json(f"/committee/{chamber}/{committee_code}")

    def list_members(self) -> Iterable[Dict[str, Any]]:
        return self.iter_paginated("/member", params={}, items_key="members")

    def get_member(self, bioguide_id: str) -> Dict[str, Any]:
        return self.get_json(f"/member/{bioguide_id}")


def _uniq_list(xs: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in xs:
        x = (x or "").strip()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _extract_bill_object(bill_detail_payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(bill_detail_payload, dict):
        return {}
    b = bill_detail_payload.get("bill")
    if isinstance(b, dict):
        return b
    # Fallback: find first dict-ish value that looks like bill
    for k in ("data", "results"):
        v = bill_detail_payload.get(k)
        if isinstance(v, dict) and any(x in v for x in ("congress", "number", "type", "titles")):
            return v
    return {}


def _pick_latest_summary(summary_payload: Dict[str, Any]) -> str:
    for key in ("summaries", "summary"):
        items = summary_payload.get(key)
        if isinstance(items, list) and items:
            # Prefer most recently updated if present.
            items_sorted = sorted(
                (x for x in items if isinstance(x, dict)),
                key=lambda d: (d.get("updateDate") or d.get("actionDate") or d.get("date") or ""),
                reverse=True,
            )
            if items_sorted:
                text = items_sorted[0].get("text") or items_sorted[0].get("summary")
                if isinstance(text, str):
                    return text.strip()
    return ""


def _pick_titles(titles_payload: Dict[str, Any]) -> Tuple[str, str]:
    short_title = ""
    official_title = ""
    items = titles_payload.get("titles")
    if not isinstance(items, list):
        return "", ""
    for t in items:
        if not isinstance(t, dict):
            continue
        title = t.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        ttype = (t.get("titleType") or "").lower()
        # Common patterns
        if not short_title and "short title" in ttype:
            short_title = title.strip()
        if not official_title and ("official title" in ttype or ttype == "official title"):
            official_title = title.strip()
    # Fallbacks
    if not short_title and items:
        first = next((x for x in items if isinstance(x, dict) and isinstance(x.get("title"), str)), None)
        if first:
            short_title = (first.get("title") or "").strip()
    if not official_title and items:
        last = next((x for x in reversed(items) if isinstance(x, dict) and isinstance(x.get("title"), str)), None)
        if last:
            official_title = (last.get("title") or "").strip()
    return short_title, official_title


def _extract_subjects(subjects_payload: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    subjects: List[str] = []
    policy_topics: List[str] = []
    for key in ("subjects", "subject"):
        items = subjects_payload.get(key)
        if isinstance(items, list):
            for s in items:
                if not isinstance(s, dict):
                    continue
                name = s.get("name") or s.get("subject")
                if isinstance(name, str) and name.strip():
                    subjects.append(name.strip())
                pa = s.get("policyArea")
                if isinstance(pa, dict):
                    pan = pa.get("name")
                    if isinstance(pan, str) and pan.strip():
                        policy_topics.append(pan.strip())
    return _uniq_list(policy_topics), _uniq_list(subjects)


def _extract_committees(committees_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    items = committees_payload.get("committees")
    if not isinstance(items, list):
        return out
    for c in items:
        if not isinstance(c, dict):
            continue
        chamber = c.get("chamber") or c.get("committeeChamber")
        name = c.get("name") or c.get("committeeName")
        code = c.get("systemCode") or c.get("committeeCode") or c.get("code")
        sub = c.get("subcommittee") or c.get("subcommitteeName") or ""
        out.append(
            {
                "chamber": (chamber or "").title() if isinstance(chamber, str) else "",
                "committee_name": (name or "").strip() if isinstance(name, str) else "",
                "committee_code": (code or "").strip() if isinstance(code, str) else "",
                "subcommittee": (sub or "").strip() if isinstance(sub, str) else "",
            }
        )
    # Dedup by (code, sub)
    seen = set()
    deduped: List[Dict[str, str]] = []
    for c in out:
        k = (c.get("committee_code") or c.get("committee_name"), c.get("subcommittee") or "")
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)
    return deduped


def _extract_cosponsors(cosponsors_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    items = cosponsors_payload.get("cosponsors")
    if not isinstance(items, list):
        return out
    for c in items:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("fullName")
        party = c.get("party")
        state = c.get("state")
        out.append(
            {
                "name": (name or "").strip() if isinstance(name, str) else "",
                "party": (party or "").strip() if isinstance(party, str) else "",
                "state": (state or "").strip() if isinstance(state, str) else "",
            }
        )
    # remove empties
    return [x for x in out if x.get("name")]


def _extract_sponsor(bill_obj: Dict[str, Any]) -> Dict[str, str]:
    sponsor: Dict[str, str] = {"name": "", "party": "", "state": "", "bioguide_id": ""}
    # Congress.gov often provides "sponsors": [...]
    sp = bill_obj.get("sponsors")
    if isinstance(sp, list) and sp:
        sp0 = sp[0] if isinstance(sp[0], dict) else None
    elif isinstance(bill_obj.get("sponsor"), dict):
        sp0 = bill_obj.get("sponsor")
    else:
        sp0 = None
    if not isinstance(sp0, dict):
        return sponsor
    sponsor["name"] = (sp0.get("name") or sp0.get("fullName") or "").strip() if isinstance(sp0.get("name") or sp0.get("fullName"), str) else ""
    sponsor["party"] = (sp0.get("party") or "").strip() if isinstance(sp0.get("party"), str) else ""
    sponsor["state"] = (sp0.get("state") or "").strip() if isinstance(sp0.get("state"), str) else ""
    sponsor["bioguide_id"] = (sp0.get("bioguideId") or sp0.get("bioguide_id") or "").strip() if isinstance(sp0.get("bioguideId") or sp0.get("bioguide_id"), str) else ""
    return sponsor


def _latest_action_from_bill_or_actions(bill_obj: Dict[str, Any], actions_payload: Dict[str, Any]) -> Dict[str, str]:
    la = bill_obj.get("latestAction")
    if isinstance(la, dict):
        date = la.get("actionDate") or la.get("date")
        text = la.get("text")
        if isinstance(date, str) or isinstance(text, str):
            return {"date": (date or "").strip() if isinstance(date, str) else "", "text": (text or "").strip() if isinstance(text, str) else ""}

    items = actions_payload.get("actions")
    if isinstance(items, list) and items:
        items_sorted = sorted(
            (x for x in items if isinstance(x, dict)),
            key=lambda d: (d.get("actionDate") or d.get("date") or ""),
            reverse=True,
        )
        if items_sorted:
            d0 = items_sorted[0]
            return {
                "date": (d0.get("actionDate") or d0.get("date") or "").strip() if isinstance(d0.get("actionDate") or d0.get("date"), str) else "",
                "text": (d0.get("text") or "").strip() if isinstance(d0.get("text"), str) else "",
            }
    return {"date": "", "text": ""}


def infer_status_stage(latest_text: str, actions_payload: Dict[str, Any]) -> str:
    text = (latest_text or "").lower()
    actions = actions_payload.get("actions")
    all_texts: List[str] = []
    if isinstance(actions, list):
        for a in actions:
            if isinstance(a, dict) and isinstance(a.get("text"), str):
                all_texts.append(a["text"].lower())
    blob = "\n".join(all_texts + ([text] if text else []))

    if "became public law" in blob or "public law no:" in blob or "signed by president" in blob:
        return "enacted"
    if "conference" in blob or "conferees" in blob:
        return "conference"
    if "passed house" in blob or "agreed to in house" in blob or "on passage" in blob and "house" in blob:
        return "passed_house"
    if "passed senate" in blob or "agreed to in senate" in blob or "on passage" in blob and "senate" in blob:
        return "passed_senate"
    if "reported by" in blob or "ordered to be reported" in blob:
        return "reported"
    if "referred to the committee" in blob or "referred to" in blob or "committee" in blob:
        return "committee"
    if "introduced" in blob:
        return "introduced"
    return "introduced"


def _committee_influence_weight(committees: List[Dict[str, str]]) -> int:
    weights = [
        ("armed services", 20),
        ("appropriations", 20),
        ("energy and commerce", 18),
        ("energy & commerce", 18),
        ("finance", 16),
        ("ways and means", 16),
        ("foreign affairs", 14),
        ("foreign relations", 14),
        ("judiciary", 12),
        ("homeland security", 12),
        ("intelligence", 12),
        ("commerce, science, and transportation", 12),
        ("banking", 10),
        ("health, education, labor, and pensions", 10),
        ("rules", 10),
    ]
    score = 0
    for c in committees:
        name = (c.get("committee_name") or "").lower()
        for needle, w in weights:
            if needle in name:
                score = max(score, w)
    return score


def _member_years_served(member_payload: Dict[str, Any]) -> Optional[float]:
    m = member_payload.get("member")
    if not isinstance(m, dict):
        return None
    terms = m.get("terms")
    if not isinstance(terms, list) or not terms:
        return None
    starts: List[dt.date] = []
    ends: List[dt.date] = []
    for t in terms:
        if not isinstance(t, dict):
            continue
        sd = _parse_iso_date(t.get("startYear") if isinstance(t.get("startYear"), str) else None)
        ed = _parse_iso_date(t.get("endYear") if isinstance(t.get("endYear"), str) else None)
        # API sometimes returns years only; accept those.
        if isinstance(t.get("startYear"), int):
            sd = dt.date(int(t["startYear"]), 1, 1)
        if isinstance(t.get("endYear"), int):
            ed = dt.date(int(t["endYear"]), 12, 31)
        if sd:
            starts.append(sd)
        if ed:
            ends.append(ed)
    if not starts:
        return None
    start = min(starts)
    end = max(ends) if ends else dt.date.today()
    days = max(0, (end - start).days)
    return days / 365.25


def compute_lobbying_metadata(
    normalized_bill: Dict[str, Any],
    actions_payload: Dict[str, Any],
    titles: Tuple[str, str],
    sponsor_member_years: Optional[float],
) -> Tuple[int, int, str]:
    title_blob = f"{titles[0]} {titles[1]}".lower()
    is_ndaa = "national defense authorization" in title_blob or "ndaa" in title_blob
    is_approp = "appropriation" in title_blob or "appropriations" in title_blob

    latest_action = normalized_bill.get("latest_action") or {}
    latest_date = _parse_iso_date(latest_action.get("date") if isinstance(latest_action, dict) else None)
    if not latest_date:
        latest_date = _parse_iso_date(normalized_bill.get("last_updated"))

    days_since = None
    if latest_date:
        days_since = (dt.date.today() - latest_date).days

    latest_text = (latest_action.get("text") if isinstance(latest_action, dict) else "") or ""
    latest_text_l = latest_text.lower()

    floor_signals = any(
        x in latest_text_l
        for x in (
            "placed on calendar",
            "calendar",
            "suspension of the rules",
            "unanimous consent",
            "cloture",
            "rule",
            "on passage",
            "passed",
        )
    )

    actions = actions_payload.get("actions")
    all_actions_text = "\n".join(
        a.get("text", "").lower() for a in actions if isinstance(actions, list) and isinstance(a, dict) and isinstance(a.get("text"), str)
    )
    markup_signals = any(x in all_actions_text for x in ("ordered to be reported", "reported by", "markup", "hearings held"))
    conference_signals = any(x in all_actions_text for x in ("conference", "conferees"))

    # Urgency score (0-100)
    if days_since is None:
        recency_score = 10
    else:
        recency_score = max(0, 60 - (days_since * 2))
    urgency = recency_score
    urgency += 10 if floor_signals else 0
    urgency += 10 if is_ndaa or is_approp else 0
    urgency += 10 if markup_signals else 0
    urgency += 10 if conference_signals else 0

    cosponsors = normalized_bill.get("cosponsors") if isinstance(normalized_bill.get("cosponsors"), list) else []
    sponsor_party = ((normalized_bill.get("sponsor") or {}).get("party") or "").strip()
    if sponsor_party and cosponsors:
        opposite = sum(1 for c in cosponsors if isinstance(c, dict) and (c.get("party") or "").strip() and (c.get("party") or "").strip() != sponsor_party)
        pct_opp = opposite / max(1, len(cosponsors))
        urgency += 5 if pct_opp >= 0.25 else 0
    urgency = int(max(0, min(100, urgency)))

    # Influence points (0-100)
    influence = 30
    influence += _committee_influence_weight(normalized_bill.get("committees") or [])
    influence += 10 if is_ndaa or is_approp else 0
    if sponsor_member_years is not None:
        influence += int(min(20, sponsor_member_years * 1.5))
    if sponsor_party and cosponsors:
        opposite = sum(1 for c in cosponsors if isinstance(c, dict) and (c.get("party") or "").strip() and (c.get("party") or "").strip() != sponsor_party)
        pct_opp = opposite / max(1, len(cosponsors))
        influence += int(min(15, pct_opp * 50))  # up to +15 at 30%+ bipartisan
    influence = int(max(0, min(100, influence)))

    # Recommended ask type
    stage = normalized_bill.get("status_stage") or "introduced"
    if is_approp:
        ask = "appropriations_insert"
    elif stage == "introduced":
        ask = "cosponsorship"
    elif stage in ("committee", "reported"):
        ask = "report_language" if markup_signals else "oversight_letter"
    elif stage in ("passed_house", "passed_senate"):
        ask = "amendment"
    elif stage == "conference":
        ask = "report_language"
    elif stage == "enacted":
        ask = "oversight_letter"
    else:
        ask = "pilot_program"

    return urgency, influence, ask


def normalize_bill(
    bill_detail_payload: Dict[str, Any],
    summaries_payload: Dict[str, Any],
    actions_payload: Dict[str, Any],
    titles_payload: Dict[str, Any],
    subjects_payload: Dict[str, Any],
    committees_payload: Dict[str, Any],
    cosponsors_payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], Tuple[str, str]]:
    bill = _extract_bill_object(bill_detail_payload)
    congress = bill.get("congress") if isinstance(bill.get("congress"), int) else int(bill.get("congress") or 0)
    bill_type = normalize_bill_type(str(bill.get("type") or bill.get("billType") or ""))
    number = bill.get("number") if isinstance(bill.get("number"), int) else int(bill.get("number") or 0)

    short_title, official_title = _pick_titles(titles_payload)
    summary = _pick_latest_summary(summaries_payload)
    latest_action = _latest_action_from_bill_or_actions(bill, actions_payload)
    policy_topics, subjects = _extract_subjects(subjects_payload)
    sponsor = _extract_sponsor(bill)
    committees = _extract_committees(committees_payload)
    cosponsors = _extract_cosponsors(cosponsors_payload)
    status_stage = infer_status_stage(latest_action.get("text", ""), actions_payload)

    # Policy area often lives on the bill object.
    pa = bill.get("policyArea")
    if isinstance(pa, dict) and isinstance(pa.get("name"), str):
        policy_topics = _uniq_list([pa["name"]] + policy_topics)

    last_updated = ""
    for k in ("updateDateIncludingText", "updateDate", "lastUpdated"):
        v = bill.get(k)
        if isinstance(v, str) and v.strip():
            last_updated = v.strip()
            break

    normalized = {
        "bill_id": build_bill_id(bill_type, number, congress),
        "bill_type": bill_type,
        "number": int(number),
        "congress": int(congress),
        "title_short": short_title,
        "title_official": official_title,
        "summary": summary,
        "latest_action": {"date": latest_action.get("date", ""), "text": latest_action.get("text", "")},
        "status_stage": status_stage,
        "policy_topics": policy_topics,
        "subjects": subjects,
        "sponsor": sponsor,
        "cosponsors": cosponsors,
        "committees": committees,
        "last_updated": last_updated,
    }
    return normalized, (short_title, official_title)


def _filter_by_name(items: List[Dict[str, Any]], needle: str, keys: Tuple[str, ...]) -> List[Dict[str, Any]]:
    n = (needle or "").strip().lower()
    if not n:
        return items
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        blob = " ".join(str(it.get(k, "") or "") for k in keys).lower()
        if n in blob:
            out.append(it)
    return out


def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("CONGRESS_API_KEY", "").strip()
    if not api_key:
        logging.error("Missing CONGRESS_API_KEY environment variable.")
        return 2

    client = CongressGovClient(ClientConfig(api_key=api_key))
    out_dir = os.path.abspath(args.data_dir)
    os.makedirs(out_dir, exist_ok=True)

    bills_raw_path = os.path.join(out_dir, "bills_raw.json")
    bills_norm_path = os.path.join(out_dir, "bills_normalized.json")
    committees_path = os.path.join(out_dir, "committees.json")
    members_path = os.path.join(out_dir, "members.json")

    existing_norm: List[Dict[str, Any]] = _load_json(bills_norm_path, [])
    norm_by_id: Dict[str, Dict[str, Any]] = {
        b.get("bill_id"): b for b in existing_norm if isinstance(b, dict) and isinstance(b.get("bill_id"), str)
    }
    existing_raw: List[Dict[str, Any]] = _load_json(bills_raw_path, [])
    raw_by_id: Dict[str, Dict[str, Any]] = {
        b.get("bill_id"): b for b in existing_raw if isinstance(b, dict) and isinstance(b.get("bill_id"), str)
    }

    current_congress = args.congress or infer_current_congress()
    logging.info("Using congress=%s", current_congress)

    # ---- Discover bills ----
    targets: List[Tuple[int, str, int]] = []
    if args.bill:
        for token in args.bill:
            for part in re.split(r"[,\n]+", token):
                part = part.strip()
                if not part:
                    continue
                bt, num, cong = parse_bill_identifier(part)
                targets.append((cong or current_congress, bt, num))

    if args.search:
        # Attempt keyword discovery via /bill/{congress}?query=... (or /bill?query=...)
        for term in args.search:
            term = term.strip()
            if not term:
                continue
            discovered: List[Dict[str, Any]] = []
            try:
                discovered = list(client.list_bills(congress=current_congress, query=term))
            except requests.HTTPError:
                try:
                    discovered = list(client.list_bills(query=term))
                except Exception:
                    logging.warning("Search query unsupported or failed for term=%r; skipping discovery for this term.", term)
                    discovered = []
            cap = args.max_discovery_per_term
            for b in discovered[:cap]:
                cong = b.get("congress")
                bt = normalize_bill_type(str(b.get("type") or b.get("billType") or ""))
                num = b.get("number")
                if isinstance(cong, int) and isinstance(num, int) and bt:
                    targets.append((cong, bt, num))

    # Dedup targets
    seen_targets = set()
    dedup_targets: List[Tuple[int, str, int]] = []
    for t in targets:
        key = (int(t[0]), str(t[1]), int(t[2]))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        dedup_targets.append(key)
    targets = dedup_targets

    if not targets:
        logging.info("No bills specified/discovered. Use --bill and/or --search.")

    # ---- Optionally fetch committees/members ----
    if not args.json_only:
        logging.info("Fetching committees...")
        committees = list(client.list_committees())
        if args.committee:
            committees = _filter_by_name(committees, args.committee, ("name", "systemCode", "committeeCode"))
        _safe_json_dump(committees, committees_path)
        logging.info("Saved committees: %s", committees_path)

        logging.info("Fetching members...")
        members = list(client.list_members())
        if args.member:
            members = _filter_by_name(members, args.member, ("name", "fullName", "bioguideId", "state"))
        _safe_json_dump(members, members_path)
        logging.info("Saved members: %s", members_path)

    # ---- Fetch + normalize bills ----
    updated = 0
    for (cong, bt, num) in targets:
        bill_id = build_bill_id(bt, num, cong)
        logging.info("Fetching bill %s ...", bill_id)

        try:
            bill_detail = client.get_bill(cong, bt, num)
        except Exception as e:
            logging.warning("Failed bill detail %s: %s", bill_id, e)
            continue

        summaries = client.get_bill_sub(cong, bt, num, "summaries") or {}
        actions = client.get_bill_sub(cong, bt, num, "actions") or {}
        titles = client.get_bill_sub(cong, bt, num, "titles") or {}
        subjects = client.get_bill_sub(cong, bt, num, "subjects") or {}
        committees = client.get_bill_sub(cong, bt, num, "committees") or {}
        cosponsors = client.get_bill_sub(cong, bt, num, "cosponsors") or {}
        related = client.get_bill_sub(cong, bt, num, "relatedbills") or {}

        normalized, picked_titles = normalize_bill(
            bill_detail_payload=bill_detail,
            summaries_payload=summaries,
            actions_payload=actions,
            titles_payload=titles,
            subjects_payload=subjects,
            committees_payload=committees,
            cosponsors_payload=cosponsors,
        )

        sponsor_bioguide = ((normalized.get("sponsor") or {}).get("bioguide_id") or "").strip()
        sponsor_years: Optional[float] = None
        if sponsor_bioguide and not args.no_member_enrichment:
            try:
                mp = client.get_member(sponsor_bioguide)
                sponsor_years = _member_years_served(mp)
            except Exception:
                sponsor_years = None

        urgency, influence, ask = compute_lobbying_metadata(
            normalized_bill=normalized,
            actions_payload=actions,
            titles=picked_titles,
            sponsor_member_years=sponsor_years,
        )
        normalized["urgency_score"] = urgency
        normalized["influence_points"] = influence
        normalized["recommended_ask_type"] = ask

        # Save raw bundle for traceability
        raw_bundle = {
            "bill_id": bill_id,
            "fetched_at": _utcnow_iso(),
            "bill_detail": bill_detail,
            "summaries": summaries,
            "actions": actions,
            "titles": titles,
            "subjects": subjects,
            "committees": committees,
            "cosponsors": cosponsors,
            "relatedbills": related,
        }

        norm_by_id[normalized["bill_id"]] = normalized
        raw_by_id[bill_id] = raw_bundle
        updated += 1

    # Write outputs
    normalized_list = sorted(norm_by_id.values(), key=lambda b: b.get("bill_id", ""))
    raw_list = sorted(raw_by_id.values(), key=lambda b: b.get("bill_id", ""))
    _safe_json_dump(raw_list, bills_raw_path)
    _safe_json_dump(normalized_list, bills_norm_path)
    logging.info("Saved bills_raw: %s", bills_raw_path)
    logging.info("Saved bills_normalized: %s", bills_norm_path)
    logging.info("Bills updated this run: %d", updated)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract, normalize, and store Congress.gov legislative data.")
    p.add_argument("--data-dir", default="./data", help="Output directory (default: ./data)")
    p.add_argument("--congress", type=int, default=None, help="Congress number to default to (e.g., 119).")
    p.add_argument("--bill", action="append", default=[], help="Bill identifier(s), e.g. HR1234, S.567, hjres12-118. Can repeat or comma-separate.")
    p.add_argument("--search", action="append", default=[], help="Keyword-based discovery term(s). Can repeat.")
    p.add_argument("--max-discovery-per-term", type=int, default=50, help="Max discovered bills per search term (default: 50).")
    p.add_argument("--committee", default=None, help="Optional committee name filter (only affects committees.json).")
    p.add_argument("--member", default=None, help="Optional member name filter (only affects members.json).")
    p.add_argument("--json-only", action="store_true", help="Skip committees/members fetch for faster bills-only export.")
    p.add_argument("--no-member-enrichment", action="store_true", help="Do not call /member/{bioguideId} to estimate sponsor seniority.")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_arg_parser().parse_args()
    try:
        rc = run(args)
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

