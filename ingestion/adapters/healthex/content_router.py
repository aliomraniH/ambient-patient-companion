"""
content_router.py — classifies HealthEx Binary/DocumentReference/Observation
resources by content type and routes each to the right specialist.

Output contract (always returns a list of dicts):
  TEXT route   → clinical_notes table rows
  STRUCT route → existing warehouse table rows (conditions, labs, etc.)
  REF route    → media_references table rows (URL + metadata only)
"""

import re
import json
import logging
from html.parser import HTMLParser

log = logging.getLogger(__name__)


# ── Content-type classification ──────────────────────────────────────────────

TEXT_TYPES   = {"text/html", "text/rtf", "text/plain", "text/xml"}
STRUCT_TYPES = {"application/xml", "application/json", "application/fhir+json"}
REF_TYPES    = {"image/jpeg", "image/jpg", "image/png", "image/dicom",
                "audio/mpeg", "audio/wav", "video/mp4", "application/dicom",
                "application/pdf"}


def classify_content_type(content_type: str) -> str:
    """Return 'text' | 'struct' | 'ref' | 'unknown'."""
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct in TEXT_TYPES or ct.startswith("text/"):
        return "text"
    if ct in STRUCT_TYPES:
        return "struct"
    if ct in REF_TYPES or ct.startswith("image/") or ct.startswith("audio/") or ct.startswith("video/"):
        return "ref"
    return "unknown"


# ── HTML stripper ─────────────────────────────────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
    """Strips HTML tags, decodes entities, preserves line breaks."""
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip_tags = {"style", "script", "head"}
        self._block_tags = {"p", "br", "div", "li", "tr", "td", "th", "h1",
                            "h2", "h3", "h4", "h5", "h6", "section"}
        self._current_skip = None

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._current_skip = tag
        if tag in self._block_tags:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag == self._current_skip:
            self._current_skip = None
        if tag in self._block_tags:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._current_skip is None:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        lines = [l.rstrip() for l in raw.split("\n")]
        result = "\n".join(lines)
        while "\n\n\n" in result:
            result = result.replace("\n\n\n", "\n\n")
        return result.strip()


def strip_html(html: str) -> str:
    """Extract plain text from HTML, preserving paragraph structure."""
    if not html:
        return ""
    try:
        extractor = _HTMLTextExtractor()
        extractor.feed(html)
        return extractor.get_text()
    except Exception as e:
        log.warning("strip_html failed: %s — falling back to regex strip", e)
        return re.sub(r"<[^>]+>", " ", html).strip()


def strip_rtf(rtf: str) -> str:
    """Extract plain text from RTF by removing control words and groups."""
    if not rtf:
        return ""
    try:
        text = re.sub(r"\\[a-z]+\d*\s?", " ", rtf)
        text = re.sub(r"[{}]", "", text)
        text = re.sub(r"\\.", "", text)
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return rtf


# ── Canonical sanitizer (used by context compiler too) ───────────────────────

def sanitize_for_context(value) -> str:
    """
    Make any text value safe to include in a JSON-serialized context dict.
    Round-trips through json.dumps/loads to escape all special characters.
    This is the canonical fix for the ultrasound blob quote-escape crash.
    """
    if value is None:
        return ""
    try:
        return json.loads(json.dumps(str(value)))
    except Exception:
        return str(value).encode("ascii", errors="replace").decode("ascii")


def _deep_sanitize(obj):
    """Recursively sanitize all string values in a nested dict/list."""
    if isinstance(obj, dict):
        return {k: _deep_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_sanitize(item) for item in obj]
    if isinstance(obj, str):
        return sanitize_for_context(obj)
    return obj


# ── Main router ───────────────────────────────────────────────────────────────

def route_fhir_resource(resource: dict, patient_id: str) -> dict:
    """
    Route a single parsed FHIR resource to the right output bucket.

    Returns:
    {
        "route":        "text" | "struct" | "ref" | "skip",
        "resource_id":  str,
        "resource_type": str,
        "rows":         list[dict],
    }
    """
    rtype = resource.get("resourceType", "")
    rid   = resource.get("id", "unknown")

    # ── Binary ────────────────────────────────────────────────────────────────
    if rtype == "Binary":
        ct        = resource.get("contentType", "")
        route_cls = classify_content_type(ct)
        data_text = resource.get("dataAsText", "") or ""

        if route_cls == "text":
            if "html" in ct:
                clean = strip_html(data_text)
            elif "rtf" in ct:
                clean = strip_rtf(data_text)
            else:
                clean = data_text.strip()

            if not clean:
                return {"route": "skip", "resource_id": rid, "resource_type": rtype, "rows": []}

            return {
                "route": "text",
                "resource_id": rid,
                "resource_type": rtype,
                "rows": [{
                    "patient_id":    patient_id,
                    "binary_id":     rid,
                    "content_type":  ct,
                    "note_text":     sanitize_for_context(clean),
                    "note_text_raw": clean,
                    "source":        "healthex_binary",
                }]
            }

        elif route_cls == "struct":
            rows = _parse_structured_binary(data_text, rid, patient_id, ct)
            if rows:
                return {"route": "struct", "resource_id": rid, "resource_type": rtype, "rows": rows}
            return _make_ref_result(rid, rtype, patient_id, ct,
                                    url=f"Binary/{rid}", note="struct-parse-failed")

        else:
            return _make_ref_result(rid, rtype, patient_id, ct, url=f"Binary/{rid}")

    # ── DocumentReference ─────────────────────────────────────────────────────
    elif rtype == "DocumentReference":
        doc_type  = (resource.get("type", {}) or {})
        type_text = doc_type.get("text", "")
        date_str  = resource.get("date", "")
        author    = ""
        if resource.get("author"):
            author = resource["author"][0].get("display", "")

        attachments = []
        for content in resource.get("content", []):
            att = content.get("attachment", {})
            attachments.append({
                "content_type": att.get("contentType", ""),
                "url":          att.get("url", ""),
            })

        rows = [{
            "patient_id":    patient_id,
            "resource_id":   rid,
            "resource_type": rtype,
            "content_type":  "",
            "doc_ref_id":    rid,
            "doc_type":      sanitize_for_context(type_text),
            "author":        sanitize_for_context(author),
            "doc_date":      date_str or None,
            "attachments":   json.dumps(attachments),
            "source":        "healthex_documentreference",
        }]
        return {"route": "ref", "resource_id": rid, "resource_type": rtype, "rows": rows}

    # ── Observation (valueString) ──────────────────────────────────────────────
    elif rtype == "Observation":
        value_string = resource.get("valueString", "")
        if value_string:
            clean     = sanitize_for_context(value_string)
            code_text = (resource.get("code", {}) or {}).get("text", "")
            eff_dt    = resource.get("effectiveDateTime", "")
            return {
                "route": "text",
                "resource_id": rid,
                "resource_type": rtype,
                "rows": [{
                    "patient_id":    patient_id,
                    "binary_id":     rid,
                    "content_type":  "observation/valueString",
                    "note_text":     clean,
                    "note_text_raw": value_string,
                    "note_date":     eff_dt or None,
                    "note_type":     sanitize_for_context(code_text),
                    "source":        "healthex_observation",
                }]
            }

    # ── Practitioner (photo) ──────────────────────────────────────────────────
    elif rtype == "Practitioner":
        photos = resource.get("photo", [])
        if photos:
            url = photos[0].get("url", "")
            ct  = photos[0].get("contentType", "image/jpeg")
            if url:
                return _make_ref_result(rid, rtype, patient_id, ct, url=url,
                                        note="provider-photo")

    return {"route": "skip", "resource_id": rid, "resource_type": rtype, "rows": []}


def _make_ref_result(rid, rtype, patient_id, ct, url="", note="") -> dict:
    return {
        "route": "ref",
        "resource_id": rid,
        "resource_type": rtype,
        "rows": [{
            "patient_id":    patient_id,
            "resource_id":   rid,
            "resource_type": rtype,
            "content_type":  ct,
            "reference_url": url,
            "note":          note,
            "source":        "healthex_media_ref",
        }]
    }


def _parse_structured_binary(data_text: str, rid: str, patient_id: str, ct: str) -> list:
    """
    Attempt to extract structured rows from application/xml or application/json Binary.
    Returns list of row dicts, or empty list if parsing fails.
    """
    if not data_text:
        return []

    if "json" in ct:
        try:
            parsed = json.loads(data_text)
            if isinstance(parsed, dict) and "entry" in parsed:
                try:
                    from .parsers.format_d_parser import parse_fhir_bundle
                    return parse_fhir_bundle(parsed, "unknown")
                except ImportError:
                    pass
        except Exception:
            pass

    if "xml" in ct:
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(data_text)
            texts = []
            for elem in root.iter():
                if elem.tag.endswith("text") and elem.text:
                    texts.append(elem.text.strip())
            if texts:
                combined = "\n".join(t for t in texts if t)
                return [{
                    "patient_id":    patient_id,
                    "binary_id":     rid,
                    "content_type":  ct,
                    "note_text":     sanitize_for_context(combined),
                    "note_text_raw": combined,
                    "source":        "healthex_xml_binary",
                }]
        except Exception as e:
            log.warning("XML parse failed for Binary/%s: %s", rid, e)

    return []


# ── DB write helpers (asyncpg) ────────────────────────────────────────────────

def _parse_ts(value) -> object:
    """Convert an ISO datetime string or None to a Python datetime (or None)."""
    if value is None or value == "":
        return None
    if hasattr(value, "year"):
        return value
    from datetime import datetime, date, timezone
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    log.warning("_parse_ts: unparseable date value %r, storing NULL", s)
    return None


async def write_clinical_note_row(conn, row: dict) -> bool:
    """Insert one row into clinical_notes. Returns True only if a row was inserted."""
    try:
        result = await conn.execute(
            """INSERT INTO clinical_notes
                   (patient_id, binary_id, content_type, note_text,
                    note_text_raw, note_type, note_date, author, source)
               VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (patient_id, binary_id) DO NOTHING""",
            row["patient_id"],
            row.get("binary_id", ""),
            row.get("content_type", ""),
            row.get("note_text", ""),
            row.get("note_text_raw", ""),
            row.get("note_type", ""),
            _parse_ts(row.get("note_date")),
            row.get("author", ""),
            row.get("source", "healthex"),
        )
        return result == "INSERT 0 1"
    except Exception as e:
        log.warning("write_clinical_note_row failed: %s", e)
        return False


async def write_media_reference_row(conn, row: dict) -> bool:
    """Insert one row into media_references. Returns True only if a row was inserted."""
    try:
        attachments = row.get("attachments")
        if isinstance(attachments, str):
            try:
                attachments = json.loads(attachments)
            except Exception:
                attachments = None

        result = await conn.execute(
            """INSERT INTO media_references
                   (patient_id, resource_id, resource_type, content_type,
                    reference_url, doc_ref_id, doc_type, author,
                    doc_date, attachments, note, source)
               VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12)
               ON CONFLICT (patient_id, resource_id) DO NOTHING""",
            row["patient_id"],
            row.get("resource_id", ""),
            row.get("resource_type", ""),
            row.get("content_type", ""),
            row.get("reference_url", ""),
            row.get("doc_ref_id", ""),
            row.get("doc_type", ""),
            row.get("author", ""),
            _parse_ts(row.get("doc_date")),
            json.dumps(attachments) if attachments else None,
            row.get("note", ""),
            row.get("source", "healthex"),
        )
        return result == "INSERT 0 1"
    except Exception as e:
        log.warning("write_media_reference_row failed: %s", e)
        return False


async def route_and_write_resources(conn, resources: list[dict], patient_id: str) -> dict:
    """
    Route a list of FHIR resources through the content router and write
    text rows to clinical_notes, ref rows to media_references.

    Returns:
        {"notes_written": int, "refs_written": int, "skipped": int}
    """
    notes_written = 0
    refs_written  = 0
    skipped       = 0

    for resource in resources:
        result = route_fhir_resource(resource, patient_id)
        route  = result["route"]

        if route == "text":
            for row in result["rows"]:
                ok = await write_clinical_note_row(conn, row)
                if ok:
                    notes_written += 1
        elif route == "ref":
            for row in result["rows"]:
                ok = await write_media_reference_row(conn, row)
                if ok:
                    refs_written += 1
        else:
            skipped += 1

    return {
        "notes_written": notes_written,
        "refs_written":  refs_written,
        "skipped":       skipped,
    }
