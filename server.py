"""HN Hiring MCP — search Hacker News' monthly "Who is hiring?" job threads.

Exposes MCP tools so an agent (Claude Desktop, etc.) can list the monthly
threads, search real postings (with role/keyword/remote/visa/salary/stage
filters), analyze a single posting, and track applications.

Data comes from Hacker News' public Algolia API — no scraping, no login,
no ToS concerns.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hn-hiring")

HN_API = "https://hn.algolia.com/api/v1"
HN_ITEM = "https://news.ycombinator.com/item?id={id}"
_TAG_RE = re.compile(r"<[^>]+>")

# Application records persist next to this file (self-contained).
_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "applications.json")

# Tech-stack vocabulary. key = display name, values = lowercase aliases.
# Matched with word boundaries so "go" won't match "google".
_TECH: dict[str, list[str]] = {
    "Python": ["python"], "JavaScript": ["javascript", "js"], "TypeScript": ["typescript", "ts"],
    "Go": ["golang", "go"], "Rust": ["rust"], "Java": ["java"], "C++": [r"c\+\+"],
    "C#": [r"c#", "dotnet", ".net"], "Ruby": ["ruby"], "PHP": ["php"], "Scala": ["scala"],
    "Kotlin": ["kotlin"], "Swift": ["swift"], "Elixir": ["elixir"],
    "React": ["react"], "Vue": ["vue"], "Angular": ["angular"], "Svelte": ["svelte"],
    "Node.js": ["node.js", "nodejs", "node"], "Next.js": ["next.js", "nextjs"],
    "Django": ["django"], "Flask": ["flask"], "FastAPI": ["fastapi"], "Rails": ["rails"],
    "Spring": ["spring"], "PyTorch": ["pytorch"], "TensorFlow": ["tensorflow"],
    "LangChain": ["langchain"], "LlamaIndex": ["llamaindex", "llama index"],
    "LLM/AI": ["llm", "llms", "genai", "rag", "agents", "ai agent"],
    "Kubernetes": ["kubernetes", "k8s"], "Docker": ["docker"],
    "AWS": ["aws"], "GCP": ["gcp", "google cloud"], "Azure": ["azure"],
    "PostgreSQL": ["postgresql", "postgres"], "MySQL": ["mysql"], "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"], "Kafka": ["kafka"], "GraphQL": ["graphql"], "Terraform": ["terraform"],
}

# Role synonyms for fuzzy matching. A user role like "backend" expands to all
# spellings; matching is hyphen/space-insensitive ("back-end" == "back end" == "backend").
_ROLE_SYNONYMS: dict[str, list[str]] = {
    "backend": ["backend", "back end", "server side", "server-side"],
    "frontend": ["frontend", "front end", "ui engineer", "ui/ux"],
    "fullstack": ["fullstack", "full stack"],
    "ml": ["machine learning", "ml engineer", "mle"],
    "ai": ["ai engineer", "artificial intelligence", "genai", "llm", "agent"],
    "data": ["data engineer", "data scientist", "data science", "analytics"],
    "devops": ["devops", "sre", "site reliability", "platform engineer", "infrastructure"],
    "mobile": ["mobile", "ios", "android"],
    "security": ["security", "appsec", "infosec"],
    "founding": ["founding engineer", "founding"],
}

# Seniority synonyms. Matched on the HEADLINE only (the role line), so a senior
# posting that merely mentions "junior" in its body is not mis-tagged.
_LEVEL_SYNONYMS: dict[str, list[str]] = {
    "intern": ["intern", "internship"],
    "junior": ["junior", "jr", "entry level", "entry-level", "new grad", "new-grad",
               "newgrad", "graduate", "early career", "associate engineer", "entry-"],
    "mid": ["mid level", "mid-level", "intermediate"],
    "senior": ["senior", "sr"],
    "staff": ["staff"],
    "principal": ["principal"],
    "lead": ["lead", "tech lead"],
}

# US detection is CASE-SENSITIVE on purpose: country "US/USA" is uppercase, while
# the pronoun in "join us" is lowercase — so this won't false-match prose.
_US_RE = re.compile(r"\b(US|USA|U\.S\.A?\.?|United States)\b")

_VISA_NEG = ["no visa", "not able to sponsor", "cannot sponsor", "can't sponsor",
             "won't sponsor", "no sponsorship", "not sponsoring", "unable to sponsor"]

# Company type/nature signals: funding stage AND org nature. Most-specific first.
_COMPANY_PATTERNS: list[tuple[str, str]] = [
    # organization nature (useful when there's no funding stage)
    ("Nonprofit", r"\b(non-?profit|not-for-profit|501\(c\)|\bngo\b|charity)\b"),
    ("Government", r"\b(government|federal agency|public sector|gov\.|\.gov)\b"),
    # require strong academic signal; bare "academic" matched "academic conferences" (false positive)
    ("Academic", r"(\.edu\b|\buniversity\b|\bresearch (lab|institute|center|centre)\b|\bnational lab\b)"),
    # require an org-noun phrase; bare "agency" also means "autonomy/ownership" (false positive)
    ("Agency", r"\b(consultancy|consulting firm|dev shop|(software|digital|creative|marketing|design|development|staffing|recruiting)\s+agency)\b"),
    # funding stage
    ("Public", r"\b(public company|publicly traded|post-ipo|nyse|nasdaq)\b"),
    ("Series D+", r"\bseries\s*[d-z]\b"),
    ("Series C", r"\bseries\s*c\b"),
    ("Series B", r"\bseries\s*b\b"),
    ("Series A", r"\bseries\s*a\b"),
    ("Seed", r"\b(pre-seed|seed[- ]stage|seed[- ]funded|raised.*seed|seed round)\b"),
    ("YC", r"\b(y[ -]?combinator|yc[ ]?[wsf]?\d{2})\b"),
    ("Bootstrapped", r"\bbootstrapp?ed\b"),
    ("Profitable", r"\bprofitable\b"),
]

# Location signals (precision-first). A bare comma is intentionally NOT a signal —
# it falsely flagged role/skill lists like "Manager, Software Development".
_REGION_RE = re.compile(
    r"\b(remote|on-?site|hybrid|worldwide|anywhere|us|usa|u\.s\.?|uk|eu|emea|apac|"
    r"americas|canada|europe|asia|germany|sweden|netherlands|france|spain|poland|india)\b",
    re.IGNORECASE)


_URL_RE = re.compile(r"https?://[^\s)>\]\"]+")
_APPLY_HINT = re.compile(r"apply|jobs?|careers?|greenhouse|lever|ashby|workable|forms?|notion|wellfound|workatastartup", re.IGNORECASE)


def _apply_url(text: str, fallback: str) -> str:
    """A single link to act on: prefer an apply/careers URL, else the first URL,
    else the HN posting permalink (fallback)."""
    urls = list(dict.fromkeys(_URL_RE.findall(text)))  # dedup, keep order
    apply = [u for u in urls if _APPLY_HINT.search(u)]
    return (apply or urls or [fallback])[0]


def _http() -> httpx.Client:
    return httpx.Client(timeout=20.0, headers={"User-Agent": "hn-hiring-mcp"})


# Small in-process cache so repeated searches in one session don't re-download the
# same monthly thread (it only changes ~once a month).
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 300  # seconds


def _cached(key: str, produce):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = produce()
    _CACHE[key] = (now, value)
    return value


def _strip_html(text: str) -> str:
    """Turn an HN comment's HTML into plain text."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _window(text: str, start: int, end: int, pad: int = 18) -> str:
    """Return a short snippet of `text` around [start, end) as evidence."""
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return ("…" if s > 0 else "") + text[s:e].strip() + ("…" if e < len(text) else "")


def _detect_stack(text: str) -> list[str]:
    """Detect technologies in a posting (word-boundary match, de-duped, order kept)."""
    low = text.lower()
    found = []
    for name, aliases in _TECH.items():
        for a in aliases:
            # word-boundary-ish: not flanked by alnum/+/# (a trailing "." is fine, e.g. "Go.")
            if re.search(rf"(?<![a-z0-9]){a}(?![a-z0-9+#])", low):
                found.append(name)
                break
    return found


# A "120k" is NOT a salary if it's immediately followed by these nouns
# (e.g. "100k users", "50k MRR", "10k stars"). Blacklist guards against that.
_NOT_SALARY = (r"(?!\s*(?:users?|customers?|mrr|arr|downloads?|stars?|requests?|"
               r"reqs?|employees?|people|seats?|rows?|tokens?|qps|rps|loc|lines?|"
               r"signups?|installs?|companies|orgs?|clients?))")


def _parse_salary(text: str) -> tuple[Optional[dict], Optional[str]]:
    """Best-effort salary range in $K, plus the evidence snippet.

    Heuristic: drops the "401(k)" retirement plan and numbers followed by
    non-salary nouns (users / MRR / stars / ...). Returns (range|None, evidence|None).
    """
    text = re.sub(r"\b401\s*\(?k\)?", " ", text, flags=re.IGNORECASE)  # retirement plan, not salary
    nums: list[int] = []
    first_span: Optional[tuple[int, int]] = None
    patterns = [
        rf"(\d{{2,3}})\s*(?:-|–|—|to)\s*(\d{{2,3}})\s*[kK]\b{_NOT_SALARY}",  # range
        rf"(\d{{2,3}})\s*[kK]\b{_NOT_SALARY}",                              # single 180k
        r"\$\s?(\d{2,3}),\d{3}",                                            # $180,000
    ]
    for pat in patterns:
        for m in re.finditer(pat, text):
            groups = [int(g) for g in m.groups() if g]
            nums += groups
            if first_span is None:
                first_span = (m.start(), m.end())
    nums = [n for n in nums if 30 <= n <= 900]   # drop numbers that aren't plausibly salaries
    if not nums:
        return None, None
    evidence = _window(text, *first_span) if first_span else None
    return {"min": min(nums), "max": max(nums)}, evidence  # unit: $K


def _sponsors_visa(text: str) -> tuple[bool, Optional[str]]:
    """Heuristic: does the posting offer visa sponsorship? Returns (bool, evidence)."""
    low = text.lower()
    m = re.search(r"visa|sponsor", low)
    if not m:
        return False, None
    evidence = _window(text, m.start(), m.end(), pad=40)
    if any(neg in low for neg in _VISA_NEG):
        return False, evidence  # evidence shows WHY it's false (the negative phrasing)
    return True, evidence


def _detect_company_type(text: str) -> tuple[list[str], dict]:
    """Heuristic company type/nature: funding stage and/or org nature.

    Returns (labels, evidence). e.g. (['Series A', 'YC'], {'Series A': '…series A…'}).
    """
    low = text.lower()
    labels, evidence = [], {}
    for label, pat in _COMPANY_PATTERNS:
        m = re.search(pat, low)
        if m:
            labels.append(label)
            evidence[label] = _window(text, m.start(), m.end())
    return labels, evidence


_MODE_RE = re.compile(r"\b(fully remote|remote|on-?site|hybrid)\b", re.IGNORECASE)
# Real US state codes — so "React, ML" / "Engineer, AI" aren't mistaken for "City, ST".
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z.]+(?: [A-Z][a-zA-Z.]+){0,2}), ([A-Z]{2})\b")


def _find_city_state(text: str) -> Optional[str]:
    """Find a 'City, ST' where ST is a real US state code."""
    for m in _CITY_STATE_RE.finditer(text):
        if m.group(2) in _US_STATES:
            return f"{m.group(1)}, {m.group(2)}"
    return None


def _looks_like_location(seg: str) -> bool:
    """A pipe segment is a location only if it carries a real location signal."""
    return bool(0 < len(seg) <= 60 and (_REGION_RE.search(seg) or _find_city_state(seg)))


def _extract_location(text: str) -> Optional[str]:
    """Best-effort location (e.g. 'Remote (US)', 'New York, NY').

    Tries the pipe-delimited header first, then falls back to a "City, ST"
    pattern or a remote/onsite/hybrid keyword in the opening of free-text posts.
    """
    segments = [s.strip() for s in text.split("|")]
    for seg in segments[1:6]:
        if _looks_like_location(seg):
            return seg
    # Fallback for free-text posts: look only in the opening (avoid stray later mentions).
    head = text[:250]
    cs = _find_city_state(head)
    if cs:
        return cs
    m = _MODE_RE.search(head)
    if m:
        return m.group(1).title()
    return None


def _norm(s: str) -> str:
    """Lowercase and collapse hyphens to spaces (for fuzzy role matching)."""
    return re.sub(r"\s+", " ", s.lower().replace("-", " ")).strip()


def _variant_pattern(v: str) -> re.Pattern:
    """Word-boundary, space/hyphen-flexible, plural-tolerant pattern for a role term.

    'ai' -> matches the word "ai" (not "email"); 'back end' -> back-end/back end/backend;
    'agent' -> agent/agents.
    """
    parts = [re.escape(p) for p in _norm(v).split() if p]
    if not parts:
        return re.compile(r"(?!x)x")  # never matches
    return re.compile(r"\b" + r"[ -]?".join(parts) + r"s?\b")


def _match_roles(text: str, roles: list[str]) -> bool:
    """Fuzzy OR-match: True if ANY requested role (with synonyms, hyphen/space
    insensitive, word-boundary) appears in the text. Empty roles -> always True."""
    if not roles:
        return True
    low = text.lower()
    for role in roles:
        variants = _ROLE_SYNONYMS.get(_norm(role).replace(" ", ""), [])
        for v in {*variants, role}:
            if _variant_pattern(v).search(low):
                return True
    return False


def _has_term(term: str, blob: str) -> bool:
    """Word-boundary match (so 'sr' won't match 'SRE', 'entry' won't match 'entrypoint')."""
    return re.search(rf"\b{re.escape(_norm(term))}\b", blob) is not None


def _detect_level(headline: str) -> list[str]:
    """Seniority detected from the role headline (e.g. ['senior'] or ['junior'])."""
    h = _norm(headline)
    return [lvl for lvl, syns in _LEVEL_SYNONYMS.items() if any(_has_term(s, h) for s in syns)]


def _match_level(headline: str, body: str, level: str) -> bool:
    """Match requested seniority. Empty -> always True.

    The level signal may live in the headline OR body (many posts say "hiring junior
    and senior" in prose). But for junior/mid/intern searches, a post whose TITLE is a
    senior-tier role (senior/staff/principal/lead) and not also junior is excluded —
    that's what wrongly pulled in "Senior SWE … mentor junior devs" before.
    """
    if not level:
        return True
    level = level.strip().lower()
    syns = _LEVEL_SYNONYMS.get(level, [level])
    blob = _norm(f"{headline} . {body}")
    if not any(_has_term(s, blob) for s in syns):
        return False
    if level in ("intern", "junior", "mid"):
        head = _norm(headline)
        title_is_senior = any(_has_term(s, head)
                              for lv in ("senior", "staff", "principal", "lead")
                              for s in _LEVEL_SYNONYMS[lv])
        title_is_target = any(_has_term(s, head) for s in syns)
        if title_is_senior and not title_is_target:
            return False
    return True


def _is_us(text: str) -> bool:
    """US-based? Case-sensitive 'US/USA' (not 'join us') or a 'City, ST' US state."""
    return bool(_US_RE.search(text) or _find_city_state(text))


def _match_location(text: str, query: str) -> bool:
    """Comma-separated OR location match. Empty -> always True.

    Special-cased: 'remote' (anywhere it's stated) and 'us'/'usa' (precise, not 'join us').
    Other terms (cities/countries) are plain substring matches.
    """
    terms = [t.strip().lower() for t in query.split(",") if t.strip()]
    if not terms:
        return True
    low = text.lower()
    for t in terms:
        if t in ("us", "usa", "u.s.", "united states"):
            if _is_us(text):
                return True
        elif t == "remote":
            if "remote" in low:
                return True
        elif t in low:
            return True
    return False


def _recent_threads(limit: int = 6) -> list[dict]:
    """Fetch recent monthly 'Ask HN: Who is hiring?' threads (cached)."""
    def fetch():
        with _http() as c:
            r = c.get(f"{HN_API}/search_by_date", params={
                "tags": "story,author_whoishiring", "query": "hiring", "hitsPerPage": 20,
            })
            r.raise_for_status()
            hits = r.json().get("hits", [])
        out = []
        for h in hits:
            title = h.get("title", "") or ""
            if "who is hiring" in title.lower():
                out.append({
                    "thread_id": h["objectID"],
                    "title": title,
                    "date": (h.get("created_at") or "")[:10],
                })
        return out

    return _cached("threads", fetch)[:limit]


def _thread_jobs(thread_id: str) -> list[dict]:
    """Fetch top-level comments of a hiring thread, each = one job posting (cached)."""
    def fetch():
        with _http() as c:
            r = c.get(f"{HN_API}/items/{thread_id}")
            r.raise_for_status()
            data = r.json()
        return data

    data = _cached(f"thread_jobs:{thread_id}", fetch)
    jobs = []
    for child in data.get("children", []):
        text = child.get("text")
        if not text or child.get("author") is None:
            continue  # skip deleted/empty
        plain = _strip_html(text)
        if not plain:
            continue
        jobs.append({
            "id": child["id"],
            "author": child.get("author"),
            "headline": plain.split("  ")[0][:140],  # first segment is usually Company | Role | Location
            "text": plain,
            "url": HN_ITEM.format(id=child["id"]),
        })
    return jobs


# ----------------------------- MCP tools -----------------------------
@mcp.tool()
def list_hiring_threads(limit: int = 6) -> list[dict]:
    """List the recent monthly Hacker News "Who is hiring?" threads.

    Args:
        limit: how many months to return (default 6).
    Returns:
        Items with thread_id / title / date.
    """
    return _recent_threads(limit)


@mcp.tool()
def search_jobs(
    keywords: str = "",
    roles: str = "",
    level: str = "",
    location: str = "",
    remote: bool = False,
    visa: bool = False,
    min_salary: int = 0,
    company_type: str = "",
    verbose: bool = False,
    thread_id: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Search a month's "Who is hiring?" postings with rich filters.

    Each result is auto-enriched with detected tech stack, location, salary
    range ($K), visa sponsorship, and company type (funding stage or org nature).

    Args:
        keywords: space-separated; a posting must contain ALL of them (e.g. "python remote").
        roles: comma-separated role terms, matched as OR with fuzzy synonyms and
            hyphen/space-insensitivity. e.g. "backend, full-stack" matches
            "back-end", "back end", "fullstack", "full stack", etc. Known roles:
            backend, frontend, fullstack, ml, ai, data, devops, mobile, security, founding.
        level: seniority, matched on the role HEADLINE only (so a senior post mentioning
            "junior" in its body is excluded). One of: intern, junior, mid, senior, staff,
            principal, lead.
        location: comma-separated OR match. Use "remote" and/or "us" (e.g. "remote, us"
            = remote OR US-based). "us"/"usa" is precise (won't match "join us"); other
            terms (cities/countries like "Berlin", "Europe") are substring matches.
        remote: shortcut to keep only postings mentioning remote.
        visa: keep only postings that EXPLICITLY state visa sponsorship. Conservative:
            postings that would sponsor but don't say so are excluded (false negatives).
        min_salary: keep only postings whose parsed salary max >= this ($K, e.g. 150).
            Note: only ~28% of postings list a salary, so this also drops the rest.
        company_type: keep only postings whose detected type contains this
            (e.g. "Series A", "Seed", "YC", "Nonprofit", "Public").
        verbose: if True, each job also includes an `evidence` dict showing the exact
            text snippets behind salary / visa / company_type (so you can verify them).
        thread_id: a specific month's thread id; omit to use the latest month.
        limit: max results (default 20).
    Returns:
        thread, thread_id, total_in_thread, matched, and jobs (each with
        headline, location, stack, salary_k, visa, company_type, url, snippet
        [+ evidence when verbose]).
    """
    if thread_id is None:
        threads = _recent_threads(1)
        if not threads:
            return {"error": "no hiring thread found", "jobs": []}
        thread = threads[0]
        thread_id = thread["thread_id"]
    else:
        # resolve the month title for the given id (cached lookup), fall back to the id
        match = next((t for t in _recent_threads(12) if t["thread_id"] == thread_id), None)
        thread = match or {"thread_id": thread_id, "title": thread_id}

    try:
        jobs = _thread_jobs(thread_id)
    except httpx.HTTPError:
        return {"error": f"could not fetch thread {thread_id}", "jobs": []}
    terms = [t.lower() for t in keywords.split() if t.strip()]
    role_list = [r.strip() for r in roles.split(",") if r.strip()]

    matched = []
    for j in jobs:
        low = j["text"].lower()
        if terms and not all(t in low for t in terms):
            continue
        if not _match_roles(j["text"], role_list):
            continue
        if not _match_level(j["headline"], j["text"], level):
            continue
        if location and not _match_location(j["text"], location):
            continue
        if remote and "remote" not in low:
            continue
        sponsors, ev_visa = _sponsors_visa(j["text"])
        if visa and not sponsors:
            continue
        salary, ev_salary = _parse_salary(j["text"])
        if min_salary > 0 and not (salary and salary["max"] >= min_salary):
            continue
        ctype, ev_ctype = _detect_company_type(j["text"])
        if company_type and company_type.lower() not in " ".join(ctype).lower():
            continue
        row = {
            "headline": j["headline"],
            "level": _detect_level(j["headline"]),
            "location": _extract_location(j["text"]),
            "stack": _detect_stack(j["text"]),
            "salary_k": salary,
            "visa": sponsors,
            "company_type": ctype,
            "apply_url": _apply_url(j["text"], j["url"]),
            "url": j["url"],
            "author": j["author"],
            "snippet": j["text"][:400],
        }
        if verbose:
            row["evidence"] = {
                "salary": ev_salary,
                "visa": ev_visa,
                "company_type": ev_ctype,
            }
        matched.append(row)

    return {
        "thread": thread.get("title", thread_id),
        "thread_id": thread_id,
        "total_in_thread": len(jobs),
        "matched": len(matched),
        "jobs": matched[:limit],
    }


@mcp.tool()
def analyze_posting(text: str) -> dict:
    """Analyze a single job posting (or any JD text you paste).

    Args:
        text: the job description text.
    Returns:
        location, stack (technologies), salary_k (range in $K), visa (sponsorship),
        company_type (funding stage or org nature), and `evidence` — the exact text
        snippets behind salary / visa / company_type so you can verify them.
    """
    salary, ev_salary = _parse_salary(text)
    visa, ev_visa = _sponsors_visa(text)
    ctype, ev_ctype = _detect_company_type(text)
    return {
        "location": _extract_location(text),
        "stack": _detect_stack(text),
        "salary_k": salary,
        "visa": visa,
        "company_type": ctype,
        "evidence": {"salary": ev_salary, "visa": ev_visa, "company_type": ev_ctype},
    }


@mcp.tool()
def get_posting(job_id: str) -> dict:
    """Fetch one job posting in FULL, with the apply link and analysis.

    Use this after `search_jobs` when you want the complete description (the search
    `snippet` is truncated). `apply_url` is the company link to apply through, and
    `url` is the HN posting itself.

    Args:
        job_id: the posting's HN comment id (the number at the end of its url).
    Returns:
        full_text, apply_url, links (all URLs in the post), plus location / stack /
        salary_k / visa / company_type, and the HN url.
    """
    def fetch():
        with _http() as c:
            r = c.get(f"{HN_API}/items/{job_id}")
            r.raise_for_status()
            return r.json()

    try:
        data = _cached(f"item:{job_id}", fetch)
    except httpx.HTTPError:
        return {"error": f"could not fetch posting {job_id} (not found or network issue)",
                "url": HN_ITEM.format(id=job_id)}
    raw = data.get("text") or ""
    plain = _strip_html(raw)
    # include href targets from raw HTML (some links aren't shown as visible text)
    hrefs = re.findall(r'href="([^"]+)"', raw)
    link_text = plain + " " + " ".join(hrefs)
    salary, _ = _parse_salary(plain)
    visa, _ = _sponsors_visa(plain)
    ctype, _ = _detect_company_type(plain)
    return {
        "url": HN_ITEM.format(id=job_id),
        "author": data.get("author"),
        "full_text": plain,
        "apply_url": _apply_url(link_text, HN_ITEM.format(id=job_id)),
        "links": list(dict.fromkeys(_URL_RE.findall(link_text))),
        "location": _extract_location(plain),
        "stack": _detect_stack(plain),
        "salary_k": salary,
        "visa": visa,
        "company_type": ctype,
    }


@mcp.tool()
def track_application(
    job_id: str,
    company: str = "",
    status: str = "applied",
    notes: str = "",
) -> dict:
    """Record/update a job application (persisted to applications.json).

    Args:
        job_id: the HN posting comment id (the number at the end of a posting's url).
        company: company name (optional, for your own reference).
        status: e.g. applied / interviewing / rejected / offer.
        notes: free-form notes.
    Returns:
        ok and the latest stored entry.
    """
    apps = _load_apps()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = apps.get(job_id, {"job_id": job_id, "url": HN_ITEM.format(id=job_id),
                              "created_at": now})
    if company:
        entry["company"] = company
    entry["status"] = status
    if notes:
        entry["notes"] = notes
    entry["updated_at"] = now
    apps[job_id] = entry
    _save_apps(apps)
    return {"ok": True, "entry": entry}


@mcp.tool()
def list_applications(status: Optional[str] = None) -> dict:
    """List tracked applications, optionally filtered by status.

    Args:
        status: only show this status (e.g. interviewing); omit for all.
    Returns:
        count and applications (most-recently-updated first).
    """
    apps = list(_load_apps().values())
    if status:
        apps = [a for a in apps if a.get("status") == status]
    apps.sort(key=lambda a: a.get("updated_at", ""), reverse=True)
    return {"count": len(apps), "applications": apps}


def _load_apps() -> dict:
    try:
        with open(_STORE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_apps(data: dict) -> None:
    with open(_STORE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
