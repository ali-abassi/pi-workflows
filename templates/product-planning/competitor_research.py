#!/usr/bin/env python3
"""Bounded, auditable Exa retrieval for product-planning competitor evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
BLOCKED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "reddit.com", "tiktok.com",
    "x.com", "youtube.com", "g2.com", "capterra.com", "trustpilot.com",
}
SAFE_ID = re.compile(r"^[a-z][a-z0-9-]*$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_id(value: str, fallback: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not result or not result[0].isalpha():
        result = fallback
    return result[:64]


def canonical_domain(url: str) -> str:
    try:
        domain = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
    return domain


def is_candidate_url(url: str) -> bool:
    domain = canonical_domain(url)
    return bool(domain and "." in domain and not any(domain == item or domain.endswith("." + item) for item in BLOCKED_DOMAINS))


def post_json(url: str, payload: dict[str, Any], key: str, timeout: int, attempts: int = 2) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"x-api-key": key, "Content-Type": "application/json", "User-Agent": "deterministic-product-planner/2.1"},
        method="POST",
    )
    last_error: Exception | None = None
    try:
        import certifi  # type: ignore[import-not-found]
        tls_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        tls_context = ssl.create_default_context()
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=tls_context) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ValueError("Exa returned a non-object response")
            return value
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(attempt + 1)
    raise RuntimeError(f"Exa request failed after {attempts} attempts: {last_error}")


def _fixture(idea: dict[str, Any]) -> dict[str, Any]:
    retrieved = now()
    pages = [
        ("clearcall.example", "ClearCall", "https://clearcall.example/pricing", "AI answering built for service businesses. Starter $99 monthly. Features include call capture, lead qualification, and calendar booking."),
        ("frontdesk.example", "FrontDesk", "https://frontdesk.example/features", "A virtual front desk for small teams. Guided setup, shared inbox, call summaries, and human escalation are emphasized."),
        ("voiceops.example", "VoiceOps", "https://voiceops.example", "Programmable voice agents for technical operations teams. Usage pricing and workflow-builder language dominate the site."),
    ]
    sources = []
    competitors = []
    for index, (domain, name, url, text) in enumerate(pages, 1):
        source_id = f"competitor-source-{index}"
        sources.append({
            "source_id": source_id, "competitor_id": safe_id(name, f"competitor-{index}"),
            "url": url, "title": name, "domain": domain, "published_date": "unknown",
            "retrieved_at": retrieved, "retrieval_method": "fixture", "status": "success",
            "content_sha256": sha256(text), "content": text,
        })
        competitors.append({
            "competitor_id": safe_id(name, f"competitor-{index}"), "name": name,
            "domain": domain, "homepage_url": f"https://{domain}", "source_ids": [source_id],
        })
    return {
        "schema_version": "1.0", "artifact_type": "competitor-evidence", "mode": "fixture",
        "status": "complete", "retrieved_at": retrieved,
        "queries": [{"query_id": "fixture-query", "query": idea["idea"], "request_id": "fixture", "result_count": len(competitors)}],
        "competitors": competitors, "sources": sources,
        "coverage": {"competitor_count": len(competitors), "source_count": len(sources), "failed_source_count": 0, "max_competitors": len(competitors), "max_pages_per_competitor": 1, "max_characters_per_page": 6000},
        "limitations": ["Synthetic fixture evidence proves controller behavior only; it is not market evidence."],
    }


def unavailable(reason: str, mode: str = "auto") -> dict[str, Any]:
    return {
        "schema_version": "1.0", "artifact_type": "competitor-evidence", "mode": mode,
        "status": "unavailable", "retrieved_at": now(), "queries": [], "competitors": [], "sources": [],
        "coverage": {"competitor_count": 0, "source_count": 0, "failed_source_count": 0, "max_competitors": 0, "max_pages_per_competitor": 0, "max_characters_per_page": 0},
        "limitations": [reason],
    }


def collect_competitor_evidence(
    idea: dict[str, Any], *, mode: str = "auto", max_competitors: int = 6,
    max_pages_per_competitor: int = 3, max_characters_per_page: int = 8000,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    """Discover competitors, then capture bounded page text with source-level receipts."""
    if mode == "off":
        return unavailable("Competitor research was disabled by the operator.", "off")
    if mode == "fixture":
        return _fixture(idea)
    key = os.environ.get("EXA_API_KEY")
    if not key:
        if mode == "exa":
            raise RuntimeError("EXA_API_KEY is required for --research-mode exa")
        return unavailable("EXA_API_KEY was unavailable; downstream claims must remain provisional.")

    idea_text = idea["idea"].strip()
    query_texts = [
        f"Direct software and service competitors for this product idea, including official pricing and product pages: {idea_text}",
        f"Alternatives buyers currently compare for this product idea; prioritize first-party company, features, and pricing pages: {idea_text}",
    ]
    queries: list[dict[str, Any]] = []
    candidates: list[dict[str, str]] = []
    seen_domains: set[str] = set()
    for index, query in enumerate(query_texts, 1):
        response = post_json(EXA_SEARCH_URL, {
            "query": query, "type": "auto", "numResults": max_competitors,
            "moderation": True,
            "contents": {"highlights": {"query": "product offer audience pricing features proof", "maxCharacters": 1800}},
        }, key, timeout_seconds)
        results = response.get("results") if isinstance(response.get("results"), list) else []
        queries.append({"query_id": f"discovery-{index}", "query": query, "request_id": str(response.get("requestId") or "unknown"), "result_count": len(results)})
        for result in results:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "")
            domain = canonical_domain(url)
            if not is_candidate_url(url) or domain in seen_domains:
                continue
            seen_domains.add(domain)
            candidates.append({"name": str(result.get("title") or domain).strip()[:160], "url": url, "domain": domain})

    if not candidates:
        if mode == "exa":
            raise RuntimeError("Exa discovery returned no eligible competitor domains")
        return unavailable("Exa discovery returned no eligible competitor domains.", "exa")

    candidates = candidates[:max_competitors]
    urls = [item["url"] for item in candidates]
    response = post_json(EXA_CONTENTS_URL, {
        "urls": urls,
        "text": {"maxCharacters": max_characters_per_page},
        "maxAgeHours": 24,
        "livecrawlTimeout": min(timeout_seconds * 1000, 30000),
        "subpages": max(0, max_pages_per_competitor - 1),
        "subpageTarget": ["pricing", "features", "product"],
    }, key, timeout_seconds)
    results = response.get("results") if isinstance(response.get("results"), list) else []
    statuses = response.get("statuses") if isinstance(response.get("statuses"), list) else []
    status_by_url = {str(item.get("id") or item.get("url") or ""): item for item in statuses if isinstance(item, dict)}
    sources: list[dict[str, Any]] = []
    by_domain: dict[str, list[str]] = {item["domain"]: [] for item in candidates}
    per_domain: dict[str, int] = {item["domain"]: 0 for item in candidates}
    failures = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url") or result.get("id") or "")
        domain = canonical_domain(url)
        if domain not in per_domain or per_domain[domain] >= max_pages_per_competitor:
            continue
        text = str(result.get("text") or "")[:max_characters_per_page].strip()
        status = status_by_url.get(url, {}).get("status", "success" if text else "error")
        if not text or status == "error":
            failures += 1
            continue
        per_domain[domain] += 1
        source_id = f"competitor-source-{len(sources) + 1}"
        sources.append({
            "source_id": source_id, "competitor_id": safe_id(domain.split(".")[0], f"competitor-{len(sources) + 1}"),
            "url": url, "title": str(result.get("title") or domain)[:240], "domain": domain,
            "published_date": str(result.get("publishedDate") or "unknown"), "retrieved_at": now(),
            "retrieval_method": "exa-contents", "status": "success", "content_sha256": sha256(text), "content": text,
        })
        by_domain[domain].append(source_id)

    competitors = []
    for index, candidate in enumerate(candidates, 1):
        source_ids = by_domain.get(candidate["domain"], [])
        if not source_ids:
            continue
        competitors.append({
            "competitor_id": safe_id(candidate["domain"].split(".")[0], f"competitor-{index}"),
            "name": candidate["name"], "domain": candidate["domain"],
            "homepage_url": candidate["url"], "source_ids": source_ids,
        })
    status = "complete" if len(competitors) >= min(3, max_competitors) else "partial"
    if mode == "exa" and len(competitors) < 2:
        raise RuntimeError("Exa content capture produced fewer than two evidence-backed competitors")
    return {
        "schema_version": "1.0", "artifact_type": "competitor-evidence", "mode": "exa",
        "status": status, "retrieved_at": now(), "request_id": str(response.get("requestId") or "unknown"),
        "queries": queries, "competitors": competitors, "sources": sources,
        "coverage": {"competitor_count": len(competitors), "source_count": len(sources), "failed_source_count": failures, "max_competitors": max_competitors, "max_pages_per_competitor": max_pages_per_competitor, "max_characters_per_page": max_characters_per_page},
        "limitations": ([] if status == "complete" else ["Fewer than three evidence-backed competitors were captured; treat category conclusions as provisional."]),
    }


def validate_competitor_evidence(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or value.get("artifact_type") != "competitor-evidence":
        return ["competitor evidence must be a typed object"]
    if value.get("status") not in {"complete", "partial", "unavailable"}:
        errors.append("competitor evidence status is invalid")
    competitors = value.get("competitors") if isinstance(value.get("competitors"), list) else []
    sources = value.get("sources") if isinstance(value.get("sources"), list) else []
    source_ids = [item.get("source_id") for item in sources if isinstance(item, dict)]
    if len(source_ids) != len(set(source_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in source_ids):
        errors.append("competitor source IDs must be unique safe IDs")
    competitor_ids = [item.get("competitor_id") for item in competitors if isinstance(item, dict)]
    if len(competitor_ids) != len(set(competitor_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in competitor_ids):
        errors.append("competitor IDs must be unique safe IDs")
    declared_sources = set(source_ids)
    for competitor in competitors:
        if not isinstance(competitor, dict) or not competitor.get("domain") or not set(competitor.get("source_ids") or []).issubset(declared_sources):
            errors.append("every competitor must have a domain and declared evidence sources")
    for source in sources:
        if not isinstance(source, dict):
            continue
        content = source.get("content")
        if not isinstance(content, str) or not content or source.get("content_sha256") != sha256(content):
            errors.append("every captured source must preserve content with a matching SHA-256")
        if not is_candidate_url(str(source.get("url") or "")) and value.get("mode") != "fixture":
            errors.append("captured source URL is invalid or blocked")
    if value.get("status") == "complete" and len(competitors) < 3:
        errors.append("complete competitor evidence requires at least three competitors")
    return errors
