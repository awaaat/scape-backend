"""
Resume parsing: LLM-based structured extraction, with a regex heuristic
fallback so the application flow never breaks if the LLM call fails,
times out, or no API key is configured.

Design goal (unchanged): the applicant's uploaded file (PDF/DOCX) is read
into memory, parsed into structured data, and then DISCARDED — nothing
gets written to disk or object storage. Only the extracted text/fields
below are persisted on the JobApplication (and the related
EducationEntry / EmploymentEntry rows), so a data-subject-access or
deletion request never has to worry about "is there also a file sitting
in S3 somewhere".

Why LLM-first:
  PDF text extraction returns words in stream order, not visual reading
  order. Multi-column / sidebar resume layouts (which are extremely
  common) come out jumbled — regex heuristics that rely on "the line
  before/after this match" break badly on that jumbling. A semantic pass
  reconstructs the correct structure regardless of layout, at a cost of
  roughly $0.005-0.01 per resume on Haiku (see jobs/README or team docs
  for current pricing — check https://docs.claude.com for the latest).

Both paths return the SAME schema (see RESUME_SCHEMA_KEYS below) so
downstream code (serializers.py) never needs to know which path ran.
"""
import io
import json
import logging
import re
from datetime import date, datetime

import pdfplumber
import requests
from django.conf import settings
from docx import Document

logger = logging.getLogger("jobs")

MAX_RAW_TEXT_CHARS = 20_000       # keep the DB row sane; this is plain text, not a file
MAX_TEXT_CHARS_FOR_LLM = 14_000   # keep prompt cost bounded regardless of resume length

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# ---------------------------------------------------------------------------
# Text extraction (unchanged approach — never touches disk)
# ---------------------------------------------------------------------------

def extract_text(file_obj, filename):
    """Read an in-memory uploaded file and return plain text. Never touches disk."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    raw = file_obj.read()

    if ext == "pdf":
        text_parts = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                # layout=True asks pdfplumber to preserve horizontal spacing,
                # which mildly helps multi-column resumes without needing
                # bespoke column-detection logic. It's not perfect — that's
                # exactly why the LLM pass below exists.
                page_text = page.extract_text(layout=True) or page.extract_text() or ""
                text_parts.append(page_text)
        return "\n".join(text_parts)

    if ext == "docx":
        doc = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs)

    # .doc (legacy binary format) isn't supported by python-docx — there's no
    # lightweight pure-Python reader for it. Reject at the validator level
    # instead of silently returning junk here.
    raise ValueError(f"Unsupported resume format for text extraction: .{ext}")


# ---------------------------------------------------------------------------
# LLM-based structured extraction (primary path)
# ---------------------------------------------------------------------------

RESUME_EXTRACTION_PROMPT = """You are extracting structured data from a resume. The text below was extracted from a PDF or DOCX file and may be jumbled or out of visual order due to multi-column layouts — reconstruct the correct logical structure using your understanding of resume content, not the order lines appear in.

Rules:
- Only include information that is actually present in the text. Never invent or guess missing details.
- Dates: use "YYYY-MM-DD" if a full date is given, "YYYY-MM-01" if only month+year is given, "YYYY-01-01" if only a year is given, or null if unknown. For an ongoing role, set end_date to null and is_current to true.
- skills_found: list only skills/technologies explicitly mentioned in the text, lowercase, deduplicated.
- Output ONLY valid JSON matching the schema below. No markdown fences, no commentary, no preamble.

Schema:
{
  "full_name_guess": string|null,
  "emails_found": [string],
  "phones_found": [string],
  "links": {"linkedin": string|null, "github": string|null, "stackoverflow": string|null, "portfolio": string|null, "other": [string]},
  "skills_found": [string],
  "certifications": [string],
  "summary": string|null,
  "education_guesses": [
    {"school": string, "degree": string, "field_of_study": string|null, "graduation_year": int|null, "gpa": string|null}
  ],
  "employment_guesses": [
    {"company": string, "job_title": string, "start_date": string|null, "end_date": string|null, "is_current": bool, "responsibilities": string|null}
  ]
}

RESUME TEXT:
{text}
"""


def _strip_json_fences(raw):
    """Strip ```json ... ``` fences if the model wraps its output despite instructions."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped.strip()


def _parse_with_llm(text):
    """
    Calls the Anthropic API to structure the resume text.
    Returns a dict matching the schema above, or None on any failure
    (missing key, network error, bad JSON, non-2xx response) — callers
    must fall back to the regex heuristic in that case.
    """
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.info("ANTHROPIC_API_KEY not set — skipping LLM resume parsing, using regex fallback.")
        return None

    model = getattr(settings, "RESUME_PARSING_MODEL", "claude-haiku-4-5-20251001")
    prompt = RESUME_EXTRACTION_PROMPT.replace("{text}", text[:MAX_TEXT_CHARS_FOR_LLM])

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.warning("LLM resume parse request failed: %s", exc)
        return None

    if resp.status_code != 200:
        logger.warning("LLM resume parse returned HTTP %s: %s", resp.status_code, resp.text[:500])
        return None

    try:
        data = resp.json()
        content_blocks = data.get("content", [])
        text_block = next((b["text"] for b in content_blocks if b.get("type") == "text"), None)
        if not text_block:
            logger.warning("LLM resume parse response had no text block: %s", data)
            return None
        parsed = json.loads(_strip_json_fences(text_block))
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("LLM resume parse response could not be parsed as JSON: %s", exc)
        return None

    return _normalize_llm_output(parsed)


def _normalize_llm_output(parsed):
    """Defensive normalization — fill in any missing keys, coerce types, cap list sizes."""
    links = parsed.get("links") or {}
    return {
        "full_name_guess": parsed.get("full_name_guess") or None,
        "emails_found": list(dict.fromkeys(parsed.get("emails_found") or []))[:5],
        "phones_found": list(dict.fromkeys(parsed.get("phones_found") or []))[:5],
        "links": {
            "linkedin": links.get("linkedin"),
            "github": links.get("github"),
            "stackoverflow": links.get("stackoverflow"),
            "portfolio": links.get("portfolio"),
            "other": (links.get("other") or [])[:5],
        },
        "skills_found": sorted(set(s.lower() for s in (parsed.get("skills_found") or [])))[:60],
        "certifications": (parsed.get("certifications") or [])[:20],
        "summary": parsed.get("summary") or None,
        "education_guesses": (parsed.get("education_guesses") or [])[:10],
        "employment_guesses": (parsed.get("employment_guesses") or [])[:15],
    }


# ---------------------------------------------------------------------------
# Regex heuristic fallback (used only if the LLM path is unavailable/fails)
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(\+?\d[\d\s().\-]{7,}\d)")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/[^\s,;)]+", re.IGNORECASE)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[^\s,;)]+", re.IGNORECASE)
STACKOVERFLOW_RE = re.compile(r"(?:https?://)?(?:www\.)?stackoverflow\.com/[^\s,;)]+", re.IGNORECASE)

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
DATE_RANGE_RE = re.compile(
    r"(?P<start>(?:[A-Z][a-z]{2,8}\.?\s+)?(?:19|20)\d{2})\s*[-–—to]{1,4}\s*"
    r"(?P<end>(?:[A-Z][a-z]{2,8}\.?\s+)?(?:19|20)\d{2}|[Pp]resent|[Cc]urrent)"
)

DEGREE_KEYWORDS = [
    "bachelor", "b.sc", "bsc", "b.a.", "ba ", "master", "m.sc", "msc", "m.a.",
    "mba", "phd", "ph.d", "doctorate", "diploma", "associate degree",
    "high school diploma",
]

SKILL_KEYWORDS = [
    "python", "javascript", "typescript", "java", "c++", "c#", "go", "golang",
    "rust", "ruby", "php", "swift", "kotlin", "sql", "nosql", "postgresql",
    "mysql", "mongodb", "redis", "django", "flask", "fastapi", "react",
    "vue", "angular", "node.js", "nodejs", "express", "graphql", "rest api",
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform", "ci/cd",
    "git", "linux", "machine learning", "deep learning", "nlp",
    "data analysis", "data science", "excel", "power bi", "tableau",
    "figma", "adobe photoshop", "salesforce", "hubspot", "seo", "sem",
    "project management", "agile", "scrum", "communication", "leadership",
    "teamwork", "problem solving", "public speaking", "negotiation",
]


def _find_links(text):
    links = {"linkedin": None, "github": None, "stackoverflow": None, "portfolio": None, "other": []}
    if m := LINKEDIN_RE.search(text):
        links["linkedin"] = m.group(0)
    if m := GITHUB_RE.search(text):
        links["github"] = m.group(0)
    if m := STACKOVERFLOW_RE.search(text):
        links["stackoverflow"] = m.group(0)
    return links


def _find_skills(text):
    lowered = text.lower()
    found = set()
    for skill in SKILL_KEYWORDS:
        pattern = r"(?<![a-z0-9])" + re.escape(skill) + r"(?![a-z0-9])"
        if re.search(pattern, lowered):
            found.add(skill)
    return sorted(found)


def _find_education(text):
    entries = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        lowered = line.lower()
        if any(kw in lowered for kw in DEGREE_KEYWORDS):
            window = " ".join(lines[max(0, i - 1): i + 2])
            year_match = YEAR_RE.search(window)
            entries.append({
                "school": "",
                "degree": line,
                "field_of_study": None,
                "graduation_year": int(year_match.group(0)) if year_match else None,
                "gpa": None,
            })
    return entries[:10]


def _find_employment(text):
    entries = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        m = DATE_RANGE_RE.search(line)
        if not m:
            continue
        context_line = lines[i - 1] if i > 0 else ""
        is_current = m.group("end").lower() in ("present", "current")
        entries.append({
            "company": "",
            "job_title": context_line,
            "start_date": m.group("start"),
            "end_date": None if is_current else m.group("end"),
            "is_current": is_current,
            "responsibilities": None,
        })
    return entries[:15]


def _parse_with_regex(text):
    """Fallback path — same output schema as _parse_with_llm."""
    return {
        "full_name_guess": None,
        "emails_found": sorted(set(EMAIL_RE.findall(text)))[:5],
        "phones_found": sorted(set(m.strip() for m in PHONE_RE.findall(text)))[:5],
        "links": _find_links(text),
        "skills_found": _find_skills(text),
        "certifications": [],
        "summary": None,
        "education_guesses": _find_education(text),
        "employment_guesses": _find_employment(text),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_resume(file_obj, filename):
    """
    Main entry point. Returns:
      raw_text  - plain text, truncated to MAX_RAW_TEXT_CHARS
      parsed    - dict of structured guesses (see schema above), plus
                  "parsed_at" and "extraction_method" metadata keys.
    Raises ValueError for unsupported formats — caller should treat that as
    "couldn't auto-parse, application still proceeds without parsed data".
    """
    text = extract_text(file_obj, filename)
    text = text.strip()

    use_llm = getattr(settings, "RESUME_PARSING_USE_LLM", True)
    parsed = _parse_with_llm(text) if use_llm else None
    method = "llm_claude_haiku_4_5"

    if parsed is None:
        parsed = _parse_with_regex(text)
        method = "heuristic_regex_v1_fallback"

    parsed["parsed_at"] = datetime.utcnow().isoformat() + "Z"
    parsed["extraction_method"] = method

    return text[:MAX_RAW_TEXT_CHARS], parsed


# ---------------------------------------------------------------------------
# Date coercion helper — used by serializers.py when creating
# EducationEntry / EmploymentEntry rows (their DB fields are real
# DateField/PositiveSmallIntegerField columns, not free text).
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def coerce_date(value):
    """
    Best-effort conversion of a date-ish string into a date object.
    Accepts "YYYY-MM-DD", "YYYY-MM", "YYYY", "Jan 2020", "January 2020".
    Returns None if it can't confidently parse the value.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()

    # ISO-ish formats first
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return date(dt.year, dt.month, dt.day if fmt == "%Y-%m-%d" else 1)
        except ValueError:
            pass

    # "Jan 2020" / "January 2020"
    m = re.match(r"([A-Za-z]{3,9})\.?\s+(\d{4})", value)
    if m:
        month_key = m.group(1)[:3].lower()
        month = _MONTH_NAMES.get(month_key)
        if month:
            return date(int(m.group(2)), month, 1)

    # Bare year
    m = re.match(r"^(\d{4})$", value)
    if m:
        return date(int(m.group(1)), 1, 1)

    return None