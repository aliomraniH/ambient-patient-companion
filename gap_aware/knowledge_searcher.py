"""External clinical knowledge search. Wraps OpenFDA, RxNorm, PubMed."""
from __future__ import annotations

import hashlib
import logging
import sys
from typing import Any, Dict, List, Optional

import httpx

from .db import check_knowledge_cache, write_knowledge_cache

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

TTL_BY_TYPE: Dict[str, int] = {
    "drug_interaction": 720,          # 30 days
    "guideline_recommendation": 2160, # 90 days
    "lab_reference_range": 4320,      # 180 days
    "dosing_adjustment": 720,
    "contraindication": 720,
    "screening_schedule": 2160,
    "differential_diagnosis": 168,    # 7 days
}


def _cache_key(query: str, query_type: str, source: str) -> str:
    raw = f"{query_type}:{source}:{query.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


# ---------------------------------------------------------------------------
# OpenFDA adapter
# ---------------------------------------------------------------------------

async def search_openfda_interactions(drug_a: str, drug_b: str) -> List[Dict[str, Any]]:
    """Search OpenFDA drug adverse event reports for a drug pair."""
    url = "https://api.fda.gov/drug/event.json"
    params = {
        "search": (
            f'patient.drug.openfda.generic_name:"{drug_a}" '
            f'AND patient.drug.openfda.generic_name:"{drug_b}"'
        ),
        "count": "patient.reaction.reactionmeddrapt.exact",
        "limit": "5",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                results = []
                for item in data.get("results", [])[:5]:
                    results.append({
                        "source": "openfda",
                        "finding": (
                            f"Reported adverse reaction: {item.get('term', 'unknown')} "
                            f"({item.get('count', 0)} reports)"
                        ),
                        "evidence_level": "observational",
                        "relevance_score": 0.7,
                        "clinical_applicability": (
                            f"Spontaneous adverse event reports for {drug_a} + {drug_b}"
                        ),
                        "source_url": "https://open.fda.gov/apis/drug/event/",
                    })
                return results
        except Exception as exc:
            logger.warning("OpenFDA search failed: %s", exc)
            return [{
                "source": "openfda",
                "finding": f"OpenFDA unavailable: {exc}",
                "evidence_level": "any",
                "relevance_score": 0.0,
                "clinical_applicability": "N/A",
            }]
    return []


# ---------------------------------------------------------------------------
# RxNorm adapter
# ---------------------------------------------------------------------------

async def search_rxnorm_interaction(drug_a: str, drug_b: str) -> List[Dict[str, Any]]:
    """Look up known interactions via the NLM RxNorm API."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r1 = await client.get(
                f"https://rxnav.nlm.nih.gov/REST/rxcui.json?name={drug_a}&search=1"
            )
            r2 = await client.get(
                f"https://rxnav.nlm.nih.gov/REST/rxcui.json?name={drug_b}&search=1"
            )
            cui_a = r1.json().get("idGroup", {}).get("rxnormId", [None])[0]
            cui_b = r2.json().get("idGroup", {}).get("rxnormId", [None])[0]
            if not cui_a or not cui_b:
                return []

            ri = await client.get(
                f"https://rxnav.nlm.nih.gov/REST/interaction/list.json?rxcuis={cui_a}+{cui_b}"
            )
            data = ri.json()
            results = []
            for group in data.get("fullInteractionTypeGroup", []):
                for itype in group.get("fullInteractionType", []):
                    for pair in itype.get("interactionPair", []):
                        results.append({
                            "source": "rxnorm",
                            "source_url": "https://rxnav.nlm.nih.gov",
                            "finding": pair.get("description", "Interaction noted"),
                            "evidence_level": "guideline",
                            "relevance_score": 0.9,
                            "clinical_applicability": pair.get("severity", "moderate"),
                        })
            if not results:
                results.append({
                    "source": "rxnorm",
                    "finding": (
                        f"No clinically significant interaction found between "
                        f"{drug_a} (RxCUI {cui_a}) and {drug_b} (RxCUI {cui_b})."
                    ),
                    "evidence_level": "guideline",
                    "relevance_score": 0.85,
                    "clinical_applicability": (
                        "No pharmacokinetic interaction flagged by NLM RxNorm interaction API."
                    ),
                })
            return results
        except Exception as exc:
            logger.warning("RxNorm search failed: %s", exc)
            return [{
                "source": "rxnorm",
                "finding": f"RxNorm unavailable: {exc}",
                "evidence_level": "any",
                "relevance_score": 0.0,
                "clinical_applicability": "N/A",
            }]


# ---------------------------------------------------------------------------
# PubMed adapter
# ---------------------------------------------------------------------------

async def search_pubmed(query: str, max_results: int = 3) -> List[Dict[str, Any]]:
    """Search PubMed for clinical evidence."""
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            sr = await client.get(search_url, params={
                "db": "pubmed", "term": query, "retmax": max_results,
                "retmode": "json", "sort": "relevance",
            })
            ids = sr.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                return []
            fr = await client.get(fetch_url, params={
                "db": "pubmed", "id": ",".join(ids), "retmode": "json",
            })
            summaries = fr.json().get("result", {})
            results = []
            for pmid in ids:
                art = summaries.get(pmid, {})
                results.append({
                    "source": "pubmed",
                    "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "finding": art.get("title", "No title"),
                    "evidence_level": "observational",
                    "relevance_score": 0.7,
                    "clinical_applicability": f"Published: {art.get('pubdate', 'unknown')}",
                })
            return results
        except Exception as exc:
            logger.warning("PubMed search failed: %s", exc)
            return [{
                "source": "pubmed",
                "finding": f"PubMed unavailable: {exc}",
                "evidence_level": "any",
                "relevance_score": 0.0,
                "clinical_applicability": "N/A",
            }]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _parse_drug_pair(query: str) -> tuple[str, str] | None:
    """Try to extract two drug names from a query like 'drugA and drugB interaction'."""
    cleaned = query.lower().replace(" interaction", "").replace(" interactions", "")
    parts = cleaned.split(" and ")
    if len(parts) == 2:
        a, b = parts[0].strip(), parts[1].strip()
        if a and b:
            return (a, b)
    return None


async def run_knowledge_search(
    query: str,
    query_type: str,
    sources: List[str],
    patient_context: Optional[Dict[str, Any]] = None,
    max_per_source: int = 5,
    gap_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a knowledge search across requested sources with caching."""
    all_results: List[Dict[str, Any]] = []

    for source in sources:
        ck = _cache_key(query, query_type, source)
        cached = await check_knowledge_cache(ck)
        if cached:
            all_results.extend(cached)
            continue

        fresh: List[Dict[str, Any]] = []
        drug_pair = _parse_drug_pair(query)

        if source == "rxnorm" and query_type == "drug_interaction" and drug_pair:
            fresh = await search_rxnorm_interaction(drug_pair[0], drug_pair[1])
        elif source == "openfda" and query_type == "drug_interaction" and drug_pair:
            fresh = await search_openfda_interactions(drug_pair[0], drug_pair[1])
        elif source == "pubmed":
            fresh = await search_pubmed(query, max_per_source)
        # Other sources (dailymed, clinicaltrials, snomed, loinc) are
        # not yet implemented — they return empty and skip caching.

        if fresh:
            ttl = TTL_BY_TYPE.get(query_type, 720)
            await write_knowledge_cache(ck, query_type, source, fresh, ttl)
            all_results.extend(fresh)

    # Sort by relevance
    all_results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    # Determine if gap is resolved
    high_confidence = [r for r in all_results if r.get("relevance_score", 0) >= 0.8]
    gap_resolved = len(high_confidence) > 0

    if gap_resolved:
        top = high_confidence[0]
        summary = f"Gap resolved via {top['source']}: {top['finding'][:300]}"
        confidence = min(0.65 + (len(high_confidence) * 0.08), 0.95)
    else:
        summary = (
            f"Searched {', '.join(sources)} — no high-confidence results "
            f"found for: {query}"
        )
        confidence = 0.4

    return {
        "results": all_results[:15],
        "gap_resolved": gap_resolved,
        "synthesis_summary": summary,
        "confidence_after_search": confidence,
        "caveats": (
            []
            if gap_resolved
            else [
                f"No definitive clinical evidence found via {', '.join(sources)}.",
                "Provider clinical judgment required.",
            ]
        ),
    }
