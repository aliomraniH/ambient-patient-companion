"""
test_content_router.py — Tests for content_router.py and its DB integration.

CR-01–CR-08  : classify_content_type
CR-09–CR-13  : strip_html / strip_rtf
CR-14–CR-16  : sanitize_for_context / _deep_sanitize
CR-17–CR-24  : route_fhir_resource (all resource types)
CR-25–CR-26  : _extract_routable_resources (executor helper)
CR-27–CR-30  : DB writes to clinical_notes via route_and_write_resources
CR-31–CR-33  : DB writes to media_references via route_and_write_resources
CR-34–CR-36  : context_compiler reads clinical_notes + available_media
"""
import json
import pytest
import asyncio
import pytest_asyncio
import uuid

from ingestion.adapters.healthex.content_router import (
    classify_content_type,
    strip_html,
    strip_rtf,
    sanitize_for_context,
    _deep_sanitize,
    route_fhir_resource,
    route_and_write_resources,
)
from ingestion.adapters.healthex.executor import _extract_routable_resources


# ── CR-01 – CR-08: classify_content_type ─────────────────────────────────────

class TestClassifyContentType:
    def test_cr01_text_html_is_text(self):
        assert classify_content_type("text/html") == "text"

    def test_cr02_text_rtf_is_text(self):
        assert classify_content_type("text/rtf") == "text"

    def test_cr03_text_plain_is_text(self):
        assert classify_content_type("text/plain") == "text"

    def test_cr04_application_xml_is_struct(self):
        assert classify_content_type("application/xml") == "struct"

    def test_cr05_application_json_is_struct(self):
        assert classify_content_type("application/json") == "struct"

    def test_cr06_image_jpg_is_ref(self):
        assert classify_content_type("image/jpg") == "ref"

    def test_cr07_application_dicom_is_ref(self):
        assert classify_content_type("application/dicom") == "ref"

    def test_cr08_empty_string_is_unknown(self):
        assert classify_content_type("") == "unknown"

    def test_cr08b_charset_suffix_stripped(self):
        assert classify_content_type("text/html; charset=utf-8") == "text"


# ── CR-09 – CR-13: strip_html / strip_rtf ────────────────────────────────────

class TestStripMarkup:
    def test_cr09_strip_html_removes_tags(self):
        html = "<p>Patient has <b>RUQ pain</b>.</p>"
        result = strip_html(html)
        assert "<" not in result
        assert "RUQ pain" in result

    def test_cr10_strip_html_style_tag_excluded(self):
        html = "<html><head><style>body{color:red}</style></head><body>Report text</body></html>"
        result = strip_html(html)
        assert "color" not in result
        assert "Report text" in result

    def test_cr11_strip_html_preserves_paragraphs(self):
        html = "<p>Para one.</p><p>Para two.</p>"
        result = strip_html(html)
        assert "Para one" in result
        assert "Para two" in result

    def test_cr12_strip_rtf_removes_control_words(self):
        rtf = r"{\rtf1\ansi Patient name: John Doe}"
        result = strip_rtf(rtf)
        assert "John Doe" in result
        assert "\\rtf1" not in result

    def test_cr13_strip_html_empty_input(self):
        assert strip_html("") == ""

    def test_cr13b_strip_rtf_empty_input(self):
        assert strip_rtf("") == ""


# ── CR-14 – CR-16: sanitize_for_context / _deep_sanitize ────────────────────

class TestSanitizeForContext:
    def test_cr14_quotes_survive_json_round_trip(self):
        raw = 'Report: "Normal" findings'
        safe = sanitize_for_context(raw)
        # Must be JSON-serializable without raising
        serialized = json.dumps({"v": safe})
        assert json.loads(serialized)["v"] == safe

    def test_cr15_none_becomes_empty_string(self):
        assert sanitize_for_context(None) == ""

    def test_cr16_deep_sanitize_nested_dict(self):
        nested = {
            "a": 'He said "hello"',
            "b": [1, 'value "quoted"'],
            "c": {"inner": "text\x00null"},
        }
        result = _deep_sanitize(nested)
        serialized = json.dumps(result)
        assert "hello" in serialized

    def test_cr16b_non_string_values_passthrough(self):
        obj = {"count": 42, "flag": True, "items": [1, 2, 3]}
        result = _deep_sanitize(obj)
        assert result["count"] == 42
        assert result["flag"] is True


# ── CR-17 – CR-24: route_fhir_resource ───────────────────────────────────────

class TestRouteFhirResource:
    PATIENT_ID = "00000000-0000-0000-0000-000000000001"

    def test_cr17_observation_valuestring_routes_text(self):
        obs = {
            "resourceType": "Observation",
            "id": "fyEZI5WFE3",
            "valueString": "BY: Ly, Tiffany\n\nLIMITED ABDOMINAL ULTRASOUND",
            "effectiveDateTime": "2023-12-15T17:40:03Z",
            "code": {"text": "Impression"},
        }
        result = route_fhir_resource(obs, self.PATIENT_ID)
        assert result["route"] == "text"
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row["note_text"] == sanitize_for_context(obs["valueString"])
        assert row["source"] == "healthex_observation"
        json.dumps({"notes": row["note_text"]})

    def test_cr18_binary_html_routes_text_strips_tags(self):
        binary = {
            "resourceType": "Binary",
            "id": "ee9bLDer",
            "contentType": "text/html",
            "dataAsText": '<p>Patient has <b>RUQ pain</b>. Dx: "Fatty liver".</p>',
        }
        result = route_fhir_resource(binary, self.PATIENT_ID)
        assert result["route"] == "text"
        row = result["rows"][0]
        assert "<" not in row["note_text"]
        assert "RUQ pain" in row["note_text"]
        json.dumps({"notes": row["note_text"]})

    def test_cr19_binary_rtf_routes_text(self):
        binary = {
            "resourceType": "Binary",
            "id": "rtfBin01",
            "contentType": "text/rtf",
            "dataAsText": r"{\rtf1 Patient is stable.}",
        }
        result = route_fhir_resource(binary, self.PATIENT_ID)
        assert result["route"] == "text"
        assert "stable" in result["rows"][0]["note_text"]

    def test_cr20_binary_image_routes_ref(self):
        binary = {
            "resourceType": "Binary",
            "id": "imgBin01",
            "contentType": "image/jpeg",
            "dataAsText": "",
        }
        result = route_fhir_resource(binary, self.PATIENT_ID)
        assert result["route"] == "ref"
        assert result["rows"][0]["reference_url"] == "Binary/imgBin01"

    def test_cr21_binary_empty_html_routes_skip(self):
        binary = {
            "resourceType": "Binary",
            "id": "emptyBin",
            "contentType": "text/html",
            "dataAsText": "<html></html>",
        }
        result = route_fhir_resource(binary, self.PATIENT_ID)
        assert result["route"] == "skip"
        assert result["rows"] == []

    def test_cr22_document_reference_routes_ref(self):
        doc = {
            "resourceType": "DocumentReference",
            "id": "docRef01",
            "type": {"text": "Diagnostic imaging study"},
            "date": "2023-12-15",
            "author": [{"display": "Dr. Smith"}],
            "content": [{"attachment": {"contentType": "text/html", "url": "Binary/abc123"}}],
        }
        result = route_fhir_resource(doc, self.PATIENT_ID)
        assert result["route"] == "ref"
        row = result["rows"][0]
        assert row["doc_type"] == "Diagnostic imaging study"
        assert row["author"] == "Dr. Smith"
        attachments = json.loads(row["attachments"])
        assert attachments[0]["url"] == "Binary/abc123"

    def test_cr23_practitioner_photo_routes_ref(self):
        prac = {
            "resourceType": "Practitioner",
            "id": "prac01",
            "photo": [{"contentType": "image/jpg",
                       "url": "https://stanfordhealthcare.org/dr.jpg"}],
        }
        result = route_fhir_resource(prac, self.PATIENT_ID)
        assert result["route"] == "ref"
        assert "stanfordhealthcare.org" in result["rows"][0]["reference_url"]
        assert result["rows"][0]["note"] == "provider-photo"

    def test_cr24_unknown_resource_type_routes_skip(self):
        unknown = {"resourceType": "Patient", "id": "p01"}
        result = route_fhir_resource(unknown, self.PATIENT_ID)
        assert result["route"] == "skip"

    def test_cr24b_ultrasound_blob_is_json_safe(self):
        obs = {
            "resourceType": "Observation",
            "id": "ultrasound01",
            "valueString": 'BY: Ly, Tiffany\n\nDIAGNOSIS: Abdominal pain [R10.11 (ICD-10-CM)]\nFatty liver [K76.0 (ICD-10-CM)]',
            "effectiveDateTime": "2023-12-15T17:40:03Z",
            "code": {"text": "Impression"},
        }
        result = route_fhir_resource(obs, self.PATIENT_ID)
        json.dumps({"notes": [result["rows"][0]["note_text"]]})


# ── CR-25 – CR-26: _extract_routable_resources ───────────────────────────────

class TestExtractRoutableResources:
    def test_cr25_fhir_bundle_extracts_observation_with_value_string(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Observation", "id": "obs1",
                              "valueString": "Impression text"}},
                {"resource": {"resourceType": "Observation", "id": "obs2",
                              "valueQuantity": {"value": 5.4}}},
                {"resource": {"resourceType": "Binary", "id": "bin1",
                              "contentType": "text/html"}},
            ]
        }
        raw = json.dumps(bundle)
        result = _extract_routable_resources(raw)
        ids = [r["id"] for r in result]
        assert "obs1" in ids
        assert "bin1" in ids
        assert "obs2" not in ids  # numeric observation excluded

    def test_cr26_non_json_returns_empty(self):
        result = _extract_routable_resources("NOT JSON AT ALL")
        assert result == []

    def test_cr26b_list_of_resources_extracted(self):
        resources = [
            {"resourceType": "Binary", "id": "b1", "contentType": "image/jpg"},
            {"resourceType": "Practitioner", "id": "p1"},
        ]
        result = _extract_routable_resources(json.dumps(resources))
        assert len(result) == 2


# ── CR-27 – CR-33: DB integration tests ──────────────────────────────────────

@pytest_asyncio.fixture
async def content_router_patient(db_pool):
    """Create a test patient for content router tests, cleaned up after each test."""
    mrn = f"cr-test-{uuid.uuid4().hex[:8]}"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO patients
                   (mrn, first_name, last_name, birth_date, gender,
                    data_source, is_synthetic)
               VALUES ($1, 'ContentRouter', 'TestPatient', '1975-03-20', 'F', 'test', true)
               RETURNING id""",
            mrn,
        )
        pid = str(row["id"])
    yield pid
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM clinical_notes WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM media_references WHERE patient_id = $1::uuid", pid)
        await conn.execute("DELETE FROM patients WHERE id = $1::uuid", pid)


@pytest.mark.asyncio
class TestClinicalNotesDB:
    async def test_cr27_observation_valuestring_writes_to_clinical_notes(
        self, db_pool, content_router_patient
    ):
        pid = content_router_patient
        obs = {
            "resourceType": "Observation",
            "id": "us-impression-cr27",
            "valueString": "ULTRASOUND: Moderate hepatic steatosis noted.",
            "effectiveDateTime": "2023-12-15T17:40:03Z",
            "code": {"text": "Impression"},
        }
        async with db_pool.acquire() as conn:
            result = await route_and_write_resources(conn, [obs], pid)

        assert result["notes_written"] >= 1

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note_text, note_type FROM clinical_notes WHERE binary_id = $1",
                "us-impression-cr27",
            )
        assert row is not None
        assert "steatosis" in row["note_text"]
        assert row["note_type"] == "Impression"

    async def test_cr28_html_binary_writes_stripped_text(
        self, db_pool, content_router_patient
    ):
        pid = content_router_patient
        binary = {
            "resourceType": "Binary",
            "id": "html-bin-cr28",
            "contentType": "text/html",
            "dataAsText": "<p>Progress note: <b>stable</b> condition.</p>",
        }
        async with db_pool.acquire() as conn:
            result = await route_and_write_resources(conn, [binary], pid)

        assert result["notes_written"] >= 1

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note_text FROM clinical_notes WHERE binary_id = $1",
                "html-bin-cr28",
            )
        assert row is not None
        assert "<" not in row["note_text"]
        assert "stable" in row["note_text"]

    async def test_cr29_conflict_on_duplicate_binary_id_is_ignored(
        self, db_pool, content_router_patient
    ):
        pid = content_router_patient
        obs = {
            "resourceType": "Observation",
            "id": "dup-obs-cr29",
            "valueString": "First write.",
            "code": {"text": "Note"},
        }
        async with db_pool.acquire() as conn:
            await route_and_write_resources(conn, [obs], pid)
            result2 = await route_and_write_resources(conn, [obs], pid)

        assert result2["notes_written"] == 0

    async def test_cr30_image_binary_skips_clinical_notes(
        self, db_pool, content_router_patient
    ):
        pid = content_router_patient
        binary = {
            "resourceType": "Binary",
            "id": "img-bin-cr30",
            "contentType": "image/jpeg",
            "dataAsText": "",
        }
        async with db_pool.acquire() as conn:
            result = await route_and_write_resources(conn, [binary], pid)

        assert result["notes_written"] == 0
        assert result["refs_written"] >= 1


@pytest.mark.asyncio
class TestMediaReferencesDB:
    async def test_cr31_practitioner_photo_writes_to_media_references(
        self, db_pool, content_router_patient
    ):
        pid = content_router_patient
        prac = {
            "resourceType": "Practitioner",
            "id": "prac-cr31",
            "photo": [{"contentType": "image/jpg",
                       "url": "https://example.com/dr-smith.jpg"}],
        }
        async with db_pool.acquire() as conn:
            result = await route_and_write_resources(conn, [prac], pid)

        assert result["refs_written"] >= 1

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT reference_url, note FROM media_references WHERE resource_id = $1",
                "prac-cr31",
            )
        assert row is not None
        assert "example.com" in row["reference_url"]
        assert row["note"] == "provider-photo"

    async def test_cr32_document_reference_writes_to_media_references(
        self, db_pool, content_router_patient
    ):
        pid = content_router_patient
        doc = {
            "resourceType": "DocumentReference",
            "id": "docref-cr32",
            "type": {"text": "Imaging study"},
            "date": "2023-12-15",
            "content": [{"attachment": {
                "contentType": "text/html",
                "url": "Binary/abc123def",
            }}],
        }
        async with db_pool.acquire() as conn:
            result = await route_and_write_resources(conn, [doc], pid)

        assert result["refs_written"] >= 1

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT doc_type, attachments FROM media_references WHERE resource_id = $1",
                "docref-cr32",
            )
        assert row is not None
        assert row["doc_type"] == "Imaging study"
        atts = row["attachments"]
        if isinstance(atts, str):
            atts = json.loads(atts)
        assert any("abc123def" in str(a.get("url", "")) for a in atts)

    async def test_cr33_mixed_resources_routes_correctly(
        self, db_pool, content_router_patient
    ):
        pid = content_router_patient
        resources = [
            {
                "resourceType": "Observation",
                "id": "mixed-obs-cr33",
                "valueString": "Gallstones noted.",
                "code": {"text": "Finding"},
            },
            {
                "resourceType": "Practitioner",
                "id": "mixed-prac-cr33",
                "photo": [{"contentType": "image/jpg", "url": "https://img.example.com/doc.jpg"}],
            },
        ]
        async with db_pool.acquire() as conn:
            result = await route_and_write_resources(conn, resources, pid)

        assert result["notes_written"] >= 1
        assert result["refs_written"] >= 1


# ── CR-34 – CR-36: context_compiler reads new tables ─────────────────────────

@pytest.mark.asyncio
class TestContextCompilerNewFields:
    async def test_cr34_compile_patient_context_has_clinical_notes_field(
        self, db_pool, content_router_patient
    ):
        from server.deliberation.context_compiler import compile_patient_context

        class _FakeVS:
            async def similarity_search(self, **kw):
                return []

        pid = content_router_patient
        ctx = await compile_patient_context(pid, db_pool, _FakeVS())
        assert hasattr(ctx, "clinical_notes")
        assert isinstance(ctx.clinical_notes, list)

    async def test_cr35_compile_patient_context_has_available_media_field(
        self, db_pool, content_router_patient
    ):
        from server.deliberation.context_compiler import compile_patient_context

        class _FakeVS:
            async def similarity_search(self, **kw):
                return []

        pid = content_router_patient
        ctx = await compile_patient_context(pid, db_pool, _FakeVS())
        assert hasattr(ctx, "available_media")
        assert isinstance(ctx.available_media, list)

    async def test_cr36_context_with_note_is_json_safe(
        self, db_pool, content_router_patient
    ):
        from server.deliberation.context_compiler import compile_patient_context

        pid = content_router_patient
        obs = {
            "resourceType": "Observation",
            "id": f"ctx-obs-cr36-{uuid.uuid4().hex[:6]}",
            "valueString": 'Report: "Fatty liver" noted. ICD [K76.0]',
            "code": {"text": "Impression"},
        }
        async with db_pool.acquire() as conn:
            await route_and_write_resources(conn, [obs], pid)

        class _FakeVS:
            async def similarity_search(self, **kw):
                return []

        ctx = await compile_patient_context(pid, db_pool, _FakeVS())
        ctx_dict = ctx.model_dump()
        serialized = json.dumps(ctx_dict)
        assert len(serialized) > 100
        assert "Fatty liver" in serialized or "K76.0" in serialized
