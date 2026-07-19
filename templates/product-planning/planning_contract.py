#!/usr/bin/env python3
"""Typed contracts and mechanical gates for the product-planning runtime."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


LANES = (
    "product-strategy",
    "user-experience",
    "information-architecture",
    "brand-and-visual-direction",
    "copy-and-conversion",
    "technical-and-operational",
)
REQUIRED_PAGE_STATES = {"normal", "loading", "empty", "error", "success", "permission-denied"}
COMMON_FIELDS = {"artifact_id", "artifact_type", "summary", "decisions", "open_questions"}
FORBIDDEN_KEYS = {"application_code", "source_code", "files_to_write", "deploy_command", "deployment_command"}
SAFE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
COPY_CHANNELS = {"landing", "product", "checkout", "onboarding", "account", "support"}
COPY_STAGES = {"exposure", "attention", "comprehension", "relevance", "belief", "motivation", "action", "successful-outcome"}
COPY_JOBS = {"orient", "promise", "explain", "prove", "distinguish", "answer", "reassure", "warn", "status", "direct"}
COPY_CHANNEL_JOBS = {
    "landing": {"orient", "promise", "explain", "prove", "answer", "direct"},
    "product": {"orient", "status", "reassure", "direct"},
    "checkout": {"orient", "explain", "warn", "reassure", "direct"},
    "onboarding": {"orient", "explain", "reassure", "direct"},
    "account": {"orient", "status", "warn", "reassure", "direct"},
    "support": {"orient", "explain", "reassure", "direct"},
}
COPY_KINDS = {"audience-language", "messaging-architecture", "claim-ledger", "copy-pack", "copy-consistency", "copy-test-plan", "copy-approval"}
PROHIBITED_COPY = re.compile(r"\b(?:guaranteed|risk[- ]free|limited time|act now|best[- ]in[- ]class|everyone is using)\b", re.I)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def validate_idea(value: Any) -> dict[str, Any]:
    allowed = {
        "name", "idea", "target_users", "business_model", "evidence", "constraints", "non_goals",
        "design_preferences", "brand_references", "brand_anti_references", "framework_constraints",
        "asset_requirements", "copy_constraints", "locales", "required_channels",
        "audience_language_evidence", "claim_evidence", "stack_overrides",
    }
    if not isinstance(value, dict):
        raise ValueError("idea must be a JSON object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("unsupported idea fields: " + ", ".join(unknown))
    idea = value.get("idea")
    if not isinstance(idea, str) or len(idea.strip()) < 20:
        raise ValueError("idea.idea must contain at least 20 characters")
    for field in (
        "target_users", "evidence", "constraints", "non_goals", "design_preferences",
        "brand_references", "brand_anti_references", "framework_constraints", "asset_requirements",
        "copy_constraints", "locales", "required_channels",
    ):
        entries = value.get(field, [])
        if not isinstance(entries, list) or not all(isinstance(item, str) and item.strip() for item in entries):
            raise ValueError(f"idea.{field} must be an array of non-empty strings")
    evidence_contracts = {
        "audience_language_evidence": ({"audience", "phrase", "source"}, {"audience", "phrase", "source"}),
        "claim_evidence": ({"claim", "source"}, {"claim", "source", "qualification", "freshness"}),
    }
    for field, (required, permitted) in evidence_contracts.items():
        entries = value.get(field, [])
        if not isinstance(entries, list) or any(
            not isinstance(item, dict)
            or not required.issubset(item)
            or not set(item).issubset(permitted)
            or any(not isinstance(item[key], str) or not item[key].strip() for key in required)
            for item in entries
        ):
            raise ValueError(f"idea.{field} must contain sourced evidence objects")
    if not set(value.get("required_channels", [])).issubset(COPY_CHANNELS):
        raise ValueError("idea.required_channels must contain declared copy channels")
    if "stack_overrides" in value and (
        not isinstance(value["stack_overrides"], dict)
        or not all(isinstance(key, str) and key and isinstance(item, str) and item for key, item in value["stack_overrides"].items())
    ):
        raise ValueError("idea.stack_overrides must map strings to strings")
    return value


def contract_for(kind: str) -> dict[str, Any]:
    common = {
        "artifact_id": "safe kebab-case stable ID",
        "artifact_type": f"exactly {kind!r}",
        "summary": "concise decision summary",
        "decisions": [{"id": "safe ID", "decision": "explicit choice", "rationale": "why", "evidence_status": "supplied|inference|assumption", "implications": ["consequence"]}],
        "open_questions": [{"id": "safe ID", "question": "unresolved question", "owner": "who resolves it", "blocking": False}],
    }
    specific: dict[str, Any] = {
        "brief": {
            "product_thesis": "one contestable thesis",
            "target_users": [{"id": "safe ID", "description": "specific user and moment", "priority": "primary|secondary"}],
            "problem": "current struggle without invented evidence",
            "desired_outcome": "observable human outcome",
            "assumptions": [{"id": "safe ID", "claim": "assumption", "risk": "high|medium|low", "falsification": "observable disproof"}],
            "success_signals": [{"id": "safe ID", "signal": "observable signal", "status": "hypothesis|known"}],
            "non_goals": ["explicit exclusion"],
        },
        "lane": {
            "lane": "one declared lane ID",
            "findings": [{"id": "safe ID", "finding": "insight", "evidence_status": "supplied|inference|assumption", "implication": "planning consequence"}],
            "recommendations": [{"id": "safe ID", "recommendation": "specific decision", "tradeoff": "what is sacrificed", "acceptance_evidence": "future proof"}],
            "risks": [{"id": "safe ID", "risk": "risk", "mitigation": "response", "owner": "owner"}],
            "dependencies": ["upstream or downstream dependency"],
        },
        "product-definition": {
            "product_thesis": "selected thesis",
            "target_user": "primary user and moment",
            "core_job": "job and progress",
            "wedge": "why this earns use instead of alternatives",
            "scope": {"mvp": ["capability"], "later": ["deferred capability"], "non_goals": ["exclusion"]},
            "experience_principles": [{"id": "safe ID", "rule": "decision rule", "failure_mode": "what it prevents"}],
            "business_model": {"model": "model", "buyer": "buyer", "value_metric": "value metric", "assumptions": ["assumption"]},
            "success_metrics": [{"id": "safe ID", "metric": "outcome metric", "why": "causal relevance", "status": "hypothesis|known"}],
        },
        "market-and-pricing": {
            "market_frame": {"category": "category", "buyer": "economic buyer", "user": "primary user", "incumbent": "what they do today", "trigger": "moment that creates urgency"},
            "latent_pain_points": [{"id": "safe ID", "actor": "person affected", "trigger": "when it appears", "surface_pain": "what they notice", "latent_pain": "deeper progress blocker", "consequence": "cost of staying put", "current_alternative": "current workaround", "evidence_status": "supplied|observed|inference|assumption", "evidence_refs": ["source reference or explicit unavailable"], "confidence": "high|medium|low", "falsification": "observable evidence that would disprove it"}],
            "switching_case": {"gain": "why switch", "burdens": ["habit, learning, migration, financial, or trust burden"], "trigger": "why now", "transition": "smallest reversible adoption path"},
            "pricing": {"currency": "ISO currency", "market": "launch market", "options": [{"id": "safe ID", "model": "subscription|usage|hybrid|one-time|free", "price_hypothesis": "exact price or range", "included": ["included value"], "limits": ["material limit"], "buyer": "buyer", "value_metric": "metric", "rationale": "value and alternative logic", "evidence_status": "supplied|verified|inference|assumption", "risk": "risk", "falsification": "observable disproof"}], "selected_option_id": "one option ID", "publication_status": "approved-to-publish|hypothesis-do-not-publish", "material_terms": ["renewal, cancellation, limits, taxes, or other material term"], "unit_economics_assumptions": ["labeled cost or margin assumption"]},
            "validation_plan": [{"id": "safe ID", "hypothesis_id": "pain or pricing ID", "method": "ethical method", "success_signal": "predeclared signal", "failure_action": "what changes"}],
        },
        "competitive-landscape": {
            "research_status": "complete|partial|unavailable",
            "source_ids": ["competitor evidence source ID"],
            "competitors": [{"competitor_id": "safe ID", "name": "competitor", "category": "category or type", "audience": "apparent audience", "offer": "observed offer", "pricing": {"observed": "observed price or unavailable", "evidence_status": "observed|unavailable", "source_ids": ["source ID"]}, "promise": "observed promise", "mechanism": "apparent mechanism", "proof": ["observed proof or explicit unavailable"], "route_patterns": ["observed site/content pattern"], "brand_cues": ["observable visual or verbal cue"], "strengths": ["evidence-linked strength"], "weaknesses": ["bounded inference"], "source_ids": ["source ID"], "evidence_status": "observed|inference"}],
            "category_patterns": [{"id": "safe ID", "pattern": "repeated category pattern", "source_ids": ["source ID"], "implication": "meaning for the product"}],
            "table_stakes": ["capability or trust signal buyers will expect"],
            "whitespace": [{"id": "safe ID", "opportunity": "specific unoccupied or weakly occupied position", "evidence": "evidence and inference boundary", "source_ids": ["source ID"], "risk": "why the opening may be misleading"}],
            "avoid_copying": ["competitor phrase, visual surface, or IA pattern not to imitate"],
            "positioning_implications": ["decision-ready positioning implication"],
            "pricing_implications": ["decision-ready pricing implication with publication boundary"],
            "research_gaps": [{"id": "safe ID", "gap": "missing evidence", "impact": "decision affected", "resolution": "next evidence action"}],
        },
        "design-direction": {
            "taste_read": "artifact/register, audience, moment, intended tone, and failure mode",
            "quality_floor": ["accessibility, responsiveness, real-content, and state requirement"],
            "theses": [{"id": "safe ID", "belief": "generative point of view", "priority": "what it privileges", "sacrifice": "what it gives up", "organizing_rule": "structural rule", "system_implications": ["implication"], "riskiest_assumption": "assumption", "proof": "observable validation"}],
            "selected_thesis_id": "one thesis ID",
            "selection_rationale": "tradeoff-based decision",
            "visual_system": {"layout": ["rule"], "type": ["rule"], "color": ["rule"], "geometry": ["rule"], "imagery": ["rule"], "iconography": ["rule"], "motion": ["rule"]},
            "interaction_system": ["feedback, control, recovery, or state rule"],
            "responsive_system": ["content-driven adaptation rule"],
            "accessibility_system": ["measurable accessibility requirement"],
            "signature_move": "one ownable subject-derived move",
            "cut_list": ["generic or competing device to avoid"],
            "reference_briefs": [{"reference": "reference or category grammar", "take": "principle to learn", "avoid_copying": "surface to avoid imitating"}],
        },
        "brand-system": {
            "positioning": {"audience": "specific audience", "category": "category", "promise": "credible promise", "mechanism": "how", "proof_needed": ["proof"], "alternatives": ["alternative"]},
            "brand_thesis": "one distinct point of view",
            "personality": [{"trait": "trait", "behavior": "observable expression", "not": "adjacent failure mode"}],
            "voice": {"principles": ["rule"], "vocabulary": ["preferred term"], "banned_language": ["generic or misleading phrase"], "tone_by_moment": [{"moment": "moment", "tone": "tone", "example": "short example"}]},
            "naming": {"product_name": "working or final name", "status": "working|validated|final", "rationale": "why", "domain_and_trademark_check": "required|complete|not-applicable"},
            "tagline": {"text": "line", "job": "reader job", "evidence_status": "supplied|inference|assumption|not-a-claim"},
            "identity": {"logo_direction": "concept and constraints", "marks": ["wordmark|symbol|lockup"], "color_roles": [{"token": "semantic token", "purpose": "purpose", "value_direction": "direction, not fake final hex", "contrast_requirement": "target"}], "type_roles": [{"role": "display|body|mono", "direction": "type direction", "fallback": "fallback", "licensing": "license requirement"}], "imagery": ["art-direction rule"], "icons": ["icon rule"], "motion": ["brand-motion rule"]},
            "governance": {"do": ["rule"], "dont": ["rule"], "approval_owner": "owner", "review_triggers": ["trigger"]},
        },
        "brand-deck": {
            "communication_job": "By the end, the implementation team should understand and consistently apply the selected brand because the deck connects audience, competitive whitespace, positioning, identity, voice, and product expression.",
            "audience": "internal product, design, copy, and engineering team",
            "central_takeaway": "one sentence brand thesis",
            "deck_title": "brand name plus brand system",
            "deck_subtitle": "specific positioning line",
            "theme": {"background": "#RRGGBB", "surface": "#RRGGBB", "primary": "#RRGGBB", "accent": "#RRGGBB", "text": "#RRGGBB", "muted": "#RRGGBB", "display_font": "installed or broadly available family", "body_font": "installed or broadly available family", "visual_motif": "one ownable motif"},
            "slides": [{"slide_id": "safe ID", "slide_type": "cover|audience|competitive-whitespace|positioning|personality|voice|logo|color|typography|imagery|product-expression|governance", "title": "takeaway title", "eyebrow": "short section label", "body": "concise audience-facing explanation", "bullets": ["zero to five concise bullets"], "callout": "optional concise callout or empty string", "source_ids": ["competitor source ID when research informed the slide"], "visual_direction": "specific composition or asset direction"}],
            "source_ids": ["competitor evidence source ID used anywhere in the deck"],
            "asset_handoff": [{"asset_id": "safe ID", "slide_ids": ["slide ID"], "production_brief": "implementation-ready asset brief", "format": "format", "owner": "owner"}],
            "rendering": {"format": "pptx", "canvas": "1280x720", "minimum_body_font_pt": 16, "editable_objects": True, "renderer": "@oai/artifact-tool"},
        },
        "audience-language": {
            "voice_of_customer_status": "sourced|partial|absent",
            "audiences": [{"audience_id": "safe ID", "situation": "specific moment", "awareness": "what they know", "intent": "what they intend", "emotion": "relevant emotional context without invented pain", "vocabulary": ["term"], "objections": ["honest objection"]}],
            "language_evidence": [{"evidence_id": "safe ID", "audience_id": "audience ID", "phrase": "verbatim phrase or clearly labeled planning language", "verbatim": False, "evidence_status": "supplied|observed|inference|unknown", "source_id": "source ID or empty", "approved_use": "how it may be used", "do_not_generalize": "limit"}],
            "sources": [{"source_id": "safe ID", "type": "idea|research|analytics|support|interview|unknown", "locator": "immutable locator or explicit unavailable", "captured_at": "timestamp or not-captured", "status": "available|unavailable"}],
            "evidence_gaps": [{"id": "safe ID", "gap": "missing audience evidence", "risk": "planning consequence", "resolution": "ethical evidence collection"}],
            "research_policy": {"invent_quotes": False, "sensitive_personalization": "forbidden without legitimate consent", "minimum_provenance": "source ID for verbatim language"},
        },
        "messaging-architecture": {
            "audience_ids": ["audience ID"],
            "copy_contract": {"artifact": "product-wide messaging system", "channels": ["landing|product|checkout|onboarding|account|support"], "desired_human_outcome": "observable outcome", "next_action": "specific action and consequence", "value": "credible value", "mechanism": "how value is created", "cost_risk_alternatives": ["material consideration"], "proof_available": ["claim ID or explicit evidence gap"], "constraints": {"voice": ["rule"], "legal": ["rule"], "accessibility": ["rule"], "localization": ["rule"], "space": ["rule"], "platform": ["rule"]}, "measurement": {"primary": "outcome", "downstream": "durable outcome", "guardrails": ["guardrail"]}},
            "performance_bottleneck": {"stage": "exposure|attention|comprehension|relevance|belief|motivation|action|successful-outcome", "evidence": "evidence or explicit assumption", "why_earliest": "why later optimization waits"},
            "message_spine": {"reader_reality": "what is already true", "desired_progress": "valued progress", "value": "what improves", "mechanism": "why", "proof": "support and limits", "objection": "honest reason not to act", "action": "specific reversible next step"},
            "message_hierarchy": [{"channel": "declared channel", "primary": "primary message", "secondary": ["supporting message"], "proof": ["claim ID"], "objection": "objection", "action": "action"}],
            "terminology": [{"concept": "concept", "canonical": "approved term", "avoid": ["term"], "reason": "reason"}],
            "claims_policy": {"unsupported_claims": "block", "assumptions": "label and exclude from production-intent claims", "proof_placement": "adjacent to supported claim", "freshness": "owner and revalidation required"},
        },
        "claim-ledger": {
            "sources": [{"source_id": "safe ID", "type": "idea|research|product-contract|analytics|support|legal|unknown", "locator": "immutable locator or unavailable", "captured_at": "timestamp or not-captured", "sha256": "digest or unavailable", "status": "available|unavailable"}],
            "claims": [{"claim_id": "safe unique ID", "claim": "consequential claim", "claim_type": "factual|comparative|promise|pricing|privacy|capability|social-proof|other", "evidence_status": "supplied|verified|inference|assumption|missing", "source_ids": ["source ID"], "qualification": "required qualification or none", "owner": "owner", "freshness": "revalidation rule", "approved_channels": ["channel"], "approved_for_copy": False, "prohibited_reason": "reason or none"}],
            "material_terms": [{"term_id": "safe ID", "term": "price, renewal, data use, commitment, eligibility, or consequence", "routes": ["route ID or pending"], "required_copy": "what must be explicit", "owner": "owner"}],
            "approval_policy": {"supported_statuses": ["supplied", "verified"], "missing_source_effect": "block", "stale_effect": "block or qualify", "legal_review_triggers": ["trigger"]},
        },
        "framework-decision": {
            "requirements": [{"id": "safe ID", "requirement": "runtime/product need", "weight": "critical|high|medium|low"}],
            "candidates": [{"id": "nextjs|react-router|astro|other-safe-id", "framework": "framework", "cloudflare_adapter": "adapter or native path", "fit": ["strength"], "tradeoffs": ["tradeoff"], "risks": ["risk"], "verifications": ["pre-code check"]}],
            "selected_candidate_id": "candidate ID",
            "decision": {"framework": "framework", "router": "router", "rendering": ["route rendering rule"], "deployment": "Cloudflare deployment path", "rationale": "why", "rejected_alternatives": [{"candidate_id": "candidate ID", "reason": "reason"}]},
            "ui_foundation": {"components": "shadcn/ui or explicit alternative", "styling": "styling system", "tokens": "token ownership", "forms": "forms and validation approach", "icons": "icon source and constraints"},
            "compatibility_risks": ["edge/runtime/dependency risk"],
            "reverify_before_code": ["current-version or platform check"],
        },
        "tech-stack": {
            "cloudflare_first": True,
            "decisions": [{"layer": "frontend|runtime|database|objects|coordination|async|auth|billing|ai-gateway|text-models|image-generation|email|analytics|observability|security|testing|ci-cd|secrets", "selection": "selected technology or none", "workload": "concrete workload", "why": "rationale", "alternatives": ["alternative"], "not_use_when": "boundary", "reverify": "pre-code verification"}],
            "integration_boundaries": [{"from": "system", "to": "system", "data": "data", "trust_boundary": "validation", "failure": "behavior"}],
            "dependency_policy": ["rule minimizing runtime and supply-chain risk"],
            "local_preview": ["how local behavior matches Workers"],
            "deployment_environments": [{"environment": "local|preview|production", "isolation": "credentials/data/domain isolation", "promotion_gate": "gate"}],
            "decision_log": [{"id": "safe ID", "decision": "decision", "status": "accepted|conditional|rejected", "revisit_when": "condition"}],
        },
        "route-manifest": {
            "routes": [{"route_id": "safe unique ID", "path": "/literal-path", "name": "human name", "audience": "visitor|member|admin", "access": "public|authenticated|authorized", "job": "single page job", "copy_channel": "landing|product|checkout|onboarding|account|support", "material_terms": ["term ID"], "high_risk_actions": ["action ID"], "entry_points": ["source"], "exit_states": ["destination or completion"], "priority": "core|supporting"}],
            "sitemap": {"root_route_id": "route ID", "primary_navigation": ["route ID"], "footer_navigation": ["route ID"], "nodes": [{"route_id": "route ID", "parent_route_id": "route ID or none", "nav_label": "exact label", "reader_job": "why this page exists", "indexing": "index|noindex"}]},
        },
        "page-plan": {
            "route_id": "declared route ID", "path": "declared path", "job": "single page job",
            "design_application": {"thesis_id": "selected design thesis", "brand_rules": ["brand rule applied here"], "framework_constraints": ["framework decision affecting this page"], "signature_move": "how the signature move appears or why it does not"},
            "sections": [{"section_id": "safe ID", "purpose": "reader or user job", "components": ["component role, not implementation code"], "copy_slot_ids": ["copy slot ID"], "asset_ids": ["asset ID"]}],
            "copy_slots": [{"id": "unique safe ID", "component_id": "stable component ID", "location": "precise UI location", "states": ["page state"], "job": "orient|promise|explain|prove|distinguish|answer|reassure|warn|status|direct", "character_limit": 120, "claim_requirement": "required|optional|none", "accessibility_constraint": "constraint"}],
            "states": [{"state_id": "normal|loading|empty|error|success|permission-denied", "trigger": "when", "user_sees": "behavior", "recovery": "action", "copy_slot_ids": ["copy slot ID"]}],
            "interactions": [{"id": "safe ID", "trigger": "user action", "system_response": "response", "failure_response": "recovery", "analytics_event": "stable event"}],
            "responsive": [{"viewport": "small|medium|large", "behavior": "content-driven adaptation"}],
            "accessibility": ["specific semantic, keyboard, focus, contrast, motion, or announcement requirement"],
            "analytics": [{"event": "stable event", "question": "decision question", "properties": ["non-sensitive property"]}],
            "data_dependencies": [{"source": "system", "data": "data", "freshness": "requirement", "failure": "fallback"}],
            "acceptance_checks": ["observable behavior check"],
        },
        "copy-pack": {
            "route_id": "declared route ID", "path": "declared path", "channel": "declared copy channel",
            "copy_contract": {"audience": "specific audience", "situation": "specific moment", "awareness": "current knowledge", "intent": "current intent", "emotion": "relevant emotion", "vocabulary": ["sourced or canonical term"], "objections": ["honest objection"], "desired_human_outcome": "observable outcome", "next_action": "specific action and consequence", "value": "value", "mechanism": "mechanism", "cost_risk_alternatives": ["material consideration"], "proof_available": ["claim ID or evidence gap"], "constraints": {"voice": ["rule"], "legal": ["rule"], "accessibility": ["rule"], "localization": ["rule"], "space": ["rule"], "platform": ["rule"]}, "measurement": {"primary": "outcome", "downstream": "durable outcome", "guardrails": ["guardrail"]}},
            "performance_bottleneck": {"stage": "declared performance stage", "evidence": "evidence or assumption", "why_earliest": "reason"},
            "message_spine": {"reader_reality": "reality", "desired_progress": "progress", "value": "value", "mechanism": "mechanism", "proof": "proof and limits", "objection": "objection", "action": "action"},
            "control": [{"copy_id": "page copy slot ID", "component_id": "page component ID", "location": "precise location", "state": "page state", "text": "exact production-intent copy", "job": "declared copy job", "claim_ids": ["approved claim ID"], "action_id": "interaction ID or none", "character_limit": 120, "accessibility": "accessible-name or announcement behavior", "localization": "expansion and grammar note"}],
            "variants": [{"candidate_id": "safe ID", "mechanism": "clarity|reader-relevance|mechanism-and-proof|objection-or-risk|autonomy-supportive|narrative", "hypothesis": "one causal message hypothesis", "performance_stage": "declared stage", "target_copy_ids": ["copy ID"], "replacements": [{"copy_id": "copy ID", "text": "replacement text"}], "changed": "one mechanism", "fixed": ["offer, audience, layout, behavior, or other invariant"], "expected_movement": "primary outcome direction", "guardrail_risk": "possible regression", "falsification": "observable disproof"}],
            "truth_agency_review": {"claims_resolved": True, "material_terms_visible": True, "consequential_actions_clear": True, "reversibility_clear": True, "prohibited_patterns": []},
            "comprehension_checks": [{"method": "paraphrase|expectation|first-click|five-second-recall|objection-interview|cloze|think-aloud|accessibility|localization", "prompt": "test prompt", "success_rule": "observable success", "audience": "target participants"}],
            "unknowns": [{"id": "safe ID", "unknown": "what readers or behavior must resolve", "proof_method": "method"}],
        },
        "copy-consistency": {
            "route_ids": ["route ID"], "copy_pack_ids": ["route ID"],
            "terminology_audit": [{"concept": "concept", "canonical": "term", "deviations": [{"copy_id": "copy ID", "text": "deviation"}], "resolution": "repair or none"}],
            "promise_chain": [{"route_id": "route ID", "entry_copy_id": "copy ID", "proof_copy_ids": ["copy ID"], "action_copy_id": "copy ID", "destination": "destination", "status": "aligned|misaligned"}],
            "claim_usage": [{"claim_id": "claim ID", "copy_ids": ["copy ID"], "qualification_present": True}],
            "voice_deviations": [{"copy_id": "copy ID", "rule": "brand voice rule", "problem": "problem", "repair": "repair"}],
            "truth_agency_gate": {"status": "pass|fail", "violations": [{"copy_id": "copy ID", "rule": "rule", "repair": "repair"}]},
            "coverage": {"route_ids": ["route ID"], "states": ["page state"], "copy_ids": ["copy ID"], "claim_ids": ["claim ID"]},
        },
        "copy-test-plan": {
            "performance_claim_status": "untested-candidates",
            "comprehension_tests": [{"test_id": "safe ID", "route_id": "route ID", "method": "paraphrase|expectation|first-click|five-second-recall|objection-interview|cloze|think-aloud|accessibility|localization", "question": "question", "participants": "target readers, not insiders", "success_rule": "predeclared observable rule", "failure_action": "repair"}],
            "experiment_candidates": [{"experiment_id": "safe ID", "route_id": "route ID", "control_copy_ids": ["copy ID"], "treatment_candidate_id": "variant ID", "causal_question": "For population, changing one mechanism is expected to change outcome without harming guardrails", "one_mechanism": "mechanism", "eligibility": "population", "randomization_unit": "unit", "primary_metric": "durable outcome", "guardrails": ["guardrail"], "minimum_detectable_effect": "calculate from business relevance and baseline; not invented", "sample_requirement": "calculate with an established method", "duration": "predeclare after traffic analysis", "data_quality_checks": ["sample-ratio and instrumentation check"], "stopping_rule": "predeclared; no convenient early stop", "ship_rule": "credible net improvement with no material guardrail harm"}],
            "low_traffic_plan": [{"route_id": "route ID", "method": "qualitative method", "decision_use": "reject or refine candidates, never estimate lift"}],
            "validation_priority": [{"rank": 1, "route_id": "route ID", "risk": "largest comprehension or trust risk", "why": "reason"}],
            "winner_policy": {"llm_scores": "candidate critique only", "qualitative": "may reject or refine, not estimate lift", "experiment": "winner only within tested population, channel, offer, and period", "inconclusive": "retain control or report inconclusive"},
        },
        "asset-plan": {
            "assets": [{"asset_id": "safe unique ID", "type": "logo|icon|illustration|photo|generated-image|diagram|social-card|favicon|other", "purpose": "user or brand job", "placements": [{"route_id": "declared route ID", "location": "precise location"}], "production": {"method": "design|photograph|illustrate|generate|license", "model": "gpt-image-2|none", "prompt_brief": "subject, composition, style, constraints, exclusions, and no unsupported text", "aspect_ratios": ["ratio"], "formats": ["svg|png|jpeg|webp|avif"], "variants": ["variant"], "alt_text_rule": "rule"}, "rights": {"owner": "owner", "license": "license/provenance requirement", "consent": "consent requirement", "retention": "retention"}, "fallback": "non-generated or failure fallback", "acceptance_checks": ["visual, technical, brand, and accessibility check"]}],
            "shared_assets": ["asset ID"],
            "generation_policy": {"model": "gpt-image-2", "human_review": "required", "provenance": ["receipt field"], "moderation": "policy", "consistency": "reference/seed strategy", "text_in_images": "avoid or verify exactly"},
            "delivery": {"storage": "R2 or bundled asset rule", "optimization": ["format/size/loading rule"], "cache": "cache policy", "naming": "stable naming rule"},
        },
        "architecture": {
            "system_context": "one-system boundary",
            "framework_ref": "framework-decision artifact ID",
            "tech_stack_ref": "tech-stack artifact ID",
            "services": [{"name": "service", "use": "concrete workload", "why": "decision rationale", "not_use_when": "boundary"}],
            "data_model": [{"entity": "entity", "owner": "system of record", "sensitive_fields": ["field"], "retention": "policy"}],
            "authz": [{"actor": "actor", "resource": "resource", "action": "action", "rule": "server-enforced rule"}],
            "payments": {"flow": "flow", "webhooks": ["verified event"], "entitlement_source": "source", "idempotency": "rule"},
            "ai": {"gateway": "AI Gateway policy", "models": [{"job": "job", "model": "model", "fallback": "fallback"}], "privacy": ["rule"], "budgets": ["limit"]},
            "images": {"model": "gpt-image-2", "workflow": "generation flow", "provenance": "record", "fallback": "non-generated fallback"},
            "async_jobs": [{"job": "job", "primitive": "Queue|Workflow|Durable Object|none", "reason": "reason", "retry": "policy"}],
            "security": ["threat and control"], "observability": ["signal and alert"], "environments": ["local, preview, or production rule"],
        },
        "roadmap": {
            "milestones": [{"id": "safe ID", "outcome": "vertical outcome", "dependencies": ["milestone ID"], "deliverables": ["deliverable"], "acceptance_checks": ["proof"], "rollback": "recovery"}],
            "risk_register": [{"id": "safe ID", "risk": "risk", "likelihood": "high|medium|low", "impact": "high|medium|low", "mitigation": "response", "owner": "owner"}],
            "decision_log": [{"id": "safe ID", "decision": "decision", "status": "accepted|assumption|blocked", "revisit_when": "condition"}],
            "implementation_order": ["milestone ID"],
        },
        "consistency": {
            "issues": [{"id": "safe ID", "severity": "critical|high|medium|low", "artifact_ids": ["artifact ID"], "problem": "inconsistency", "repair": "exact repair or none"}],
            "resolved_decisions": [{"id": "safe ID", "decision": "canonical decision", "artifact_ids": ["artifact ID"]}],
            "coverage": {"route_ids": ["route ID"], "page_ids": ["route ID"], "asset_ids": ["asset ID"], "copy_pack_ids": ["route ID"], "copy_ids": ["copy ID"], "copy_line_count": 1, "state_count": 1},
        },
        "copy-approval": {
            "overall_status": "approved-for-build",
            "performance_status": "untested-candidates",
            "definition": "approved-for-build means contract-valid, truthful, coherent, and implementation-ready; it does not mean performance-proven",
            "issue_resolutions": [{"issue_id": "critical or high consistency issue ID", "resolution": "exact repair incorporated here", "canonical_decision": "single source-of-truth decision", "supersedes_artifact_ids": ["upstream artifact ID"]}],
            "routes": [{"route_id": "route ID", "path": "/literal-path", "status": "approved-for-build", "controls": [{"copy_id": "page copy slot ID", "component_id": "page component ID", "location": "precise location", "state": "page state", "text": "exact approved-for-build copy", "job": "copy job", "claim_ids": ["approved claim ID"], "action_id": "interaction ID or none", "character_limit": 120, "accessibility": "accessible behavior", "localization": "localization note"}]}],
            "source_of_truth": ["ordered precedence rule"],
        },
        "final-plan": {
            "executive_summary": "what will be built and why",
            "product_definition": "canonical product thesis and wedge",
            "route_ids": ["route ID"], "page_ids": ["route ID"],
            "market_summary": "category, buyer, incumbent, trigger, and selected latent pains", "pricing_summary": "selected pricing hypothesis and publication boundary", "sitemap_summary": "page hierarchy and navigation", "design_summary": "selected design direction", "brand_summary": "brand system", "copy_summary": "messaging and approved-for-build copy system", "framework_summary": "web framework decision", "tech_stack_summary": "Cloudflare-first stack decision", "asset_ids": ["asset ID"], "claim_ids": ["claim ID"], "copy_pack_ids": ["route ID"], "copy_ids": ["copy ID"], "copy_approval_status": "approved-for-build", "copy_test_status": "untested-candidates", "resolved_issue_ids": ["critical or high consistency issue ID"], "source_of_truth": ["ordered artifact precedence rule"],
            "architecture_summary": "system shape", "roadmap_summary": "delivery sequence",
            "artifact_index": [{"artifact_id": "artifact ID", "path": "run-relative JSON path", "sha256": "provided by controller or pending"}],
            "decision_closure": [{"id": "safe ID", "uncertainty": "what was uncertain", "decision": "strongest conservative reversible default", "rationale": "why this is the best available choice", "evidence_status": "supplied|inference|assumption", "risk": "remaining risk", "reversible": True, "fallback": "safe behavior if wrong", "revisit_trigger": "new evidence that justifies changing it"}],
            "external_verification": [{"id": "safe ID", "fact": "external fact the planner cannot know", "reason_unverifiable": "why it cannot be resolved from supplied evidence", "method": "exact verification method", "status": "required|verified|not-required", "blocking_for": "launch|production-use", "safe_fallback": "conservative no-op or restricted behavior that keeps implementation unblocked", "evidence": "verification evidence or not-yet-verified"}],
            "implementation_ready": "exactly true; planning decisions cannot be delegated to a future human",
            "launch_ready": "boolean; false only while required external verification remains",
        },
    }
    if kind not in specific:
        raise ValueError(f"unsupported artifact kind: {kind}")
    return {**common, **specific[kind]}


def _walk(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def validate_artifact(
    kind: str,
    value: Any,
    *,
    route: dict[str, Any] | None = None,
    page: dict[str, Any] | None = None,
    expected_routes: set[str] | None = None,
    expected_assets: set[str] | None = None,
    expected_audiences: set[str] | None = None,
    expected_claims: set[str] | None = None,
    approved_claims: set[str] | None = None,
    expected_copy_ids: set[str] | None = None,
    expected_variants: dict[str, set[str]] | None = None,
    expected_issue_ids: set[str] | None = None,
    expected_copy_units: dict[str, dict[str, Any]] | None = None,
    expected_source_ids: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["artifact must be an object"]
    missing = sorted(COMMON_FIELDS - set(value))
    if missing:
        errors.append("missing common fields: " + ", ".join(missing))
    if value.get("artifact_type") != kind:
        errors.append(f"artifact_type must be {kind}")
    artifact_id = value.get("artifact_id")
    if not isinstance(artifact_id, str) or not SAFE_ID.fullmatch(artifact_id):
        errors.append("artifact_id must use safe kebab-case")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        errors.append("summary must be non-empty")
    if not isinstance(value.get("decisions"), list):
        errors.append("decisions must be an array")
    if not isinstance(value.get("open_questions"), list):
        errors.append("open_questions must be an array")
    for key, item in _walk(value):
        if key in FORBIDDEN_KEYS:
            errors.append(f"forbidden application-code key: {key}")
        if isinstance(item, str) and ("```" in item or re.search(r"\b(?:TODO|TBD|lorem ipsum)\b", item, re.I)):
            errors.append(f"placeholder or code-fence content at {key}")

    required = set(contract_for(kind))
    missing_specific = sorted(required - set(value))
    if missing_specific:
        errors.append("missing contract fields: " + ", ".join(missing_specific))
    if kind == "lane" and value.get("lane") not in LANES:
        errors.append("lane must be a declared lane ID")
    if kind == "design-direction":
        theses = value.get("theses") if isinstance(value.get("theses"), list) else []
        thesis_ids = [item.get("id") for item in theses if isinstance(item, dict)]
        if len(theses) < 3 or len(thesis_ids) != len(set(thesis_ids)) or value.get("selected_thesis_id") not in thesis_ids:
            errors.append("design direction must compare at least three unique theses and select one")
    if kind == "brand-system":
        personality = value.get("personality") if isinstance(value.get("personality"), list) else []
        identity = value.get("identity") if isinstance(value.get("identity"), dict) else {}
        if len(personality) < 3 or not identity.get("marks") or not identity.get("color_roles") or not identity.get("type_roles"):
            errors.append("brand system must define at least three behavioral traits plus marks, color roles, and type roles")
    if kind == "competitive-landscape":
        source_ids = set(value.get("source_ids") or [])
        if expected_source_ids is not None and not source_ids.issubset(expected_source_ids):
            errors.append("competitive landscape source_ids must reference captured competitor evidence")
        competitors = value.get("competitors") if isinstance(value.get("competitors"), list) else []
        if value.get("research_status") in {"complete", "partial"} and len(competitors) < 2:
            errors.append("available competitor research requires at least two analyzed competitors")
        for competitor in competitors:
            refs = set(competitor.get("source_ids") or []) if isinstance(competitor, dict) else set()
            pricing_refs = set((competitor.get("pricing") or {}).get("source_ids") or []) if isinstance(competitor, dict) else set()
            if not refs or not (refs | pricing_refs).issubset(source_ids):
                errors.append("every analyzed competitor and observed price must reference captured evidence")
                break
        for item in value.get("category_patterns", []) + value.get("whitespace", []):
            if isinstance(item, dict) and not set(item.get("source_ids") or []).issubset(source_ids):
                errors.append("competitive patterns and whitespace must reference captured evidence")
                break
    if kind == "brand-deck":
        theme = value.get("theme") if isinstance(value.get("theme"), dict) else {}
        colors = [theme.get(key) for key in ("background", "surface", "primary", "accent", "text", "muted")]
        if any(not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color) for color in colors):
            errors.append("brand deck theme must define six exact hex colors")
        slides = value.get("slides") if isinstance(value.get("slides"), list) else []
        ids = [item.get("slide_id") for item in slides if isinstance(item, dict)]
        required_types = {"cover", "audience", "competitive-whitespace", "positioning", "personality", "voice", "logo", "color", "typography", "imagery", "product-expression", "governance"}
        types = {item.get("slide_type") for item in slides if isinstance(item, dict)}
        if not 12 <= len(slides) <= 14 or len(ids) != len(set(ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in ids) or not required_types.issubset(types):
            errors.append("brand deck must contain 12-14 unique slides covering every required narrative job")
        source_ids = set(value.get("source_ids") or [])
        if expected_source_ids is not None and not source_ids.issubset(expected_source_ids):
            errors.append("brand deck source_ids must reference captured competitor evidence")
        used_sources = {source_id for slide in slides if isinstance(slide, dict) for source_id in slide.get("source_ids", [])}
        if used_sources != source_ids:
            errors.append("brand deck source_ids must exactly equal slide-level research citations")
        for slide in slides:
            if not isinstance(slide, dict):
                continue
            if len(slide.get("bullets") or []) > 5 or any(len(str(item).split()) > 18 for item in slide.get("bullets") or []):
                errors.append("brand deck slides may use at most five concise bullets")
                break
            if not set(slide.get("source_ids") or []).issubset(source_ids):
                errors.append("brand deck slide citations must reference the deck source inventory")
                break
        rendering = value.get("rendering") if isinstance(value.get("rendering"), dict) else {}
        if rendering.get("format") != "pptx" or rendering.get("renderer") != "@oai/artifact-tool" or rendering.get("editable_objects") is not True:
            errors.append("brand deck must declare editable artifact-tool PPTX rendering")
    if kind == "market-and-pricing":
        pains = value.get("latent_pain_points") if isinstance(value.get("latent_pain_points"), list) else []
        pain_ids = [item.get("id") for item in pains if isinstance(item, dict)]
        if len(pains) < 3 or len(pain_ids) != len(set(pain_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in pain_ids):
            errors.append("market plan must define at least three unique, safe latent pain points")
        for pain in pains:
            if isinstance(pain, dict) and (pain.get("evidence_status") not in {"supplied", "observed", "inference", "assumption"} or not pain.get("evidence_refs") or not pain.get("falsification")):
                errors.append("every latent pain must expose evidence status, references, and falsification")
                break
        pricing = value.get("pricing") if isinstance(value.get("pricing"), dict) else {}
        options = pricing.get("options") if isinstance(pricing.get("options"), list) else []
        option_ids = [item.get("id") for item in options if isinstance(item, dict)]
        if not options or len(option_ids) != len(set(option_ids)) or pricing.get("selected_option_id") not in option_ids:
            errors.append("pricing must compare options and select exactly one declared option")
        if pricing.get("publication_status") not in {"approved-to-publish", "hypothesis-do-not-publish"}:
            errors.append("pricing must declare a valid publication boundary")
        selected = next((item for item in options if isinstance(item, dict) and item.get("id") == pricing.get("selected_option_id")), {})
        if selected.get("evidence_status") not in {"supplied", "verified"} and pricing.get("publication_status") != "hypothesis-do-not-publish":
            errors.append("inferred or assumed pricing must remain hypothesis-do-not-publish")
    if kind == "audience-language":
        audiences = value.get("audiences") if isinstance(value.get("audiences"), list) else []
        audience_ids = [item.get("audience_id") for item in audiences if isinstance(item, dict)]
        if not audience_ids or len(audience_ids) != len(set(audience_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in audience_ids):
            errors.append("audience IDs must be non-empty, unique, and safe")
        sources = {item.get("source_id") for item in value.get("sources", []) if isinstance(item, dict)}
        evidence_ids: list[Any] = []
        for item in value.get("language_evidence", []):
            if not isinstance(item, dict):
                continue
            evidence_ids.append(item.get("evidence_id"))
            if item.get("audience_id") not in set(audience_ids):
                errors.append("language evidence must reference a declared audience")
            if item.get("verbatim") is True and (item.get("evidence_status") not in {"supplied", "observed"} or item.get("source_id") not in sources):
                errors.append("verbatim audience language requires supplied or observed source provenance")
        if len(evidence_ids) != len(set(evidence_ids)):
            errors.append("language evidence IDs must be unique")
    if kind == "messaging-architecture":
        audience_ids = set(value.get("audience_ids") or [])
        if expected_audiences is not None and audience_ids != expected_audiences:
            errors.append("messaging audience_ids must exactly match the audience-language artifact")
        bottleneck = value.get("performance_bottleneck") if isinstance(value.get("performance_bottleneck"), dict) else {}
        if bottleneck.get("stage") not in COPY_STAGES:
            errors.append("messaging performance bottleneck must use a declared performance stage")
        channels = set((value.get("copy_contract") or {}).get("channels") or [])
        if not channels or not channels.issubset(COPY_CHANNELS):
            errors.append("messaging channels must be declared copy channels")
    if kind == "claim-ledger":
        sources = value.get("sources") if isinstance(value.get("sources"), list) else []
        source_ids = [item.get("source_id") for item in sources if isinstance(item, dict)]
        if len(source_ids) != len(set(source_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in source_ids):
            errors.append("claim source IDs must be unique safe IDs")
        claims = value.get("claims") if isinstance(value.get("claims"), list) else []
        claim_ids = [item.get("claim_id") for item in claims if isinstance(item, dict)]
        if not claim_ids or len(claim_ids) != len(set(claim_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in claim_ids):
            errors.append("claim IDs must be non-empty, unique, and safe")
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            refs = set(claim.get("source_ids") or [])
            channels = set(claim.get("approved_channels") or [])
            if not refs.issubset(set(source_ids)) or not channels.issubset(COPY_CHANNELS):
                errors.append("claims must reference declared sources and copy channels")
            if claim.get("approved_for_copy") is True and (claim.get("evidence_status") not in {"supplied", "verified"} or not refs):
                errors.append("copy-approved claims require supplied or verified source evidence")
    if kind == "framework-decision":
        candidates = value.get("candidates") if isinstance(value.get("candidates"), list) else []
        candidate_ids = {item.get("id") for item in candidates if isinstance(item, dict)}
        if not {"nextjs", "react-router", "astro"}.issubset(candidate_ids) or value.get("selected_candidate_id") not in candidate_ids:
            errors.append("framework decision must compare Next.js, React Router, and Astro and select a declared candidate")
    if kind == "tech-stack":
        layers = {item.get("layer") for item in value.get("decisions", []) if isinstance(item, dict)}
        required_layers = {"frontend", "runtime", "database", "objects", "coordination", "async", "auth", "billing", "ai-gateway", "text-models", "image-generation", "email", "analytics", "observability", "security", "testing", "ci-cd", "secrets"}
        if value.get("cloudflare_first") is not True or not required_layers.issubset(layers):
            errors.append("tech stack must be Cloudflare-first and decide every required layer")
    if kind == "route-manifest":
        routes = value.get("routes")
        if not isinstance(routes, list) or not routes:
            errors.append("routes must be a non-empty array")
        else:
            ids = [item.get("route_id") for item in routes if isinstance(item, dict)]
            paths = [item.get("path") for item in routes if isinstance(item, dict)]
            if len(ids) != len(routes) or len(ids) != len(set(ids)) or not all(isinstance(item, str) and SAFE_ID.fullmatch(item) for item in ids):
                errors.append("route IDs must be unique safe IDs")
            if len(paths) != len(routes) or len(paths) != len(set(paths)) or not all(isinstance(item, str) and item.startswith("/") for item in paths):
                errors.append("route paths must be unique absolute web paths")
            if any(item.get("copy_channel") not in COPY_CHANNELS for item in routes if isinstance(item, dict)):
                errors.append("every route must declare a supported copy channel")
            sitemap = value.get("sitemap") if isinstance(value.get("sitemap"), dict) else {}
            nodes = sitemap.get("nodes") if isinstance(sitemap.get("nodes"), list) else []
            node_ids = [item.get("route_id") for item in nodes if isinstance(item, dict)]
            route_ids = set(ids)
            if len(node_ids) != len(nodes) or set(node_ids) != route_ids or len(node_ids) != len(set(node_ids)):
                errors.append("sitemap nodes must cover every route exactly once")
            if sitemap.get("root_route_id") not in route_ids:
                errors.append("sitemap root must reference a declared route")
            navigation = set(sitemap.get("primary_navigation") or []) | set(sitemap.get("footer_navigation") or [])
            if not navigation.issubset(route_ids):
                errors.append("sitemap navigation must reference declared routes")
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                parent = node.get("parent_route_id")
                if parent != "none" and parent not in route_ids:
                    errors.append("sitemap parent must reference a declared route or none")
                if node.get("indexing") not in {"index", "noindex"}:
                    errors.append("sitemap indexing must be index or noindex")
    if kind == "page-plan":
        if route and (value.get("route_id") != route.get("route_id") or value.get("path") != route.get("path")):
            errors.append("page route_id/path must match the route manifest")
        slots = value.get("copy_slots")
        slot_ids = [item.get("id") for item in slots if isinstance(item, dict)] if isinstance(slots, list) else []
        if not slot_ids or len(slot_ids) != len(set(slot_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in slot_ids):
            errors.append("copy slot IDs must be non-empty, unique, and safe")
        for slot in slots if isinstance(slots, list) else []:
            if not isinstance(slot, dict):
                continue
            if slot.get("job") not in COPY_JOBS or not isinstance(slot.get("character_limit"), int) or slot["character_limit"] < 1:
                errors.append("copy slots must declare a supported job and positive character limit")
            if not set(slot.get("states") or []).issubset(REQUIRED_PAGE_STATES):
                errors.append("copy slot states must be declared page states")
        if route:
            slot_jobs = {item.get("job") for item in slots if isinstance(item, dict)} if isinstance(slots, list) else set()
            required_jobs = COPY_CHANNEL_JOBS.get(route.get("copy_channel"), set())
            if not required_jobs.issubset(slot_jobs):
                errors.append("page copy slots must satisfy the route channel's required reader jobs")
            interaction_ids = {item.get("id") for item in value.get("interactions", []) if isinstance(item, dict)}
            if not set(route.get("high_risk_actions") or []).issubset(interaction_ids):
                errors.append("page interactions must declare every route high-risk action")
        states = value.get("states")
        state_ids = {item.get("state_id") for item in states if isinstance(item, dict)} if isinstance(states, list) else set()
        if state_ids != REQUIRED_PAGE_STATES:
            errors.append("page states must be exactly: " + ", ".join(sorted(REQUIRED_PAGE_STATES)))
        references = {
            slot_id
            for collection in (value.get("sections", []), value.get("states", []))
            for item in collection if isinstance(item, dict)
            for slot_id in item.get("copy_slot_ids", [])
        }
        if not references.issubset(set(slot_ids)):
            errors.append("section/state copy_slot_ids must reference declared copy slots")
        state_slot_coverage = {item.get("state_id") for item in value.get("states", []) if isinstance(item, dict) and item.get("copy_slot_ids")}
        if state_slot_coverage != REQUIRED_PAGE_STATES:
            errors.append("every page state must reference at least one copy slot")
        asset_references = {asset_id for section in value.get("sections", []) if isinstance(section, dict) for asset_id in section.get("asset_ids", [])}
        if expected_assets is not None and not asset_references.issubset(expected_assets):
            errors.append("page asset_ids must reference the asset plan")
    if kind == "copy-pack":
        if route and (value.get("route_id") != route.get("route_id") or value.get("path") != route.get("path") or value.get("channel") != route.get("copy_channel")):
            errors.append("copy pack route, path, and channel must match the route manifest")
        control = value.get("control") if isinstance(value.get("control"), list) else []
        control_ids = [item.get("copy_id") for item in control if isinstance(item, dict)]
        page_slots = {item.get("id"): item for item in (page or {}).get("copy_slots", []) if isinstance(item, dict)}
        if not control_ids or len(control_ids) != len(set(control_ids)) or set(control_ids) != set(page_slots):
            errors.append("copy control must cover every page copy slot exactly once")
        interaction_ids = {item.get("id") for item in (page or {}).get("interactions", []) if isinstance(item, dict)}
        high_risk_actions = set((route or {}).get("high_risk_actions") or [])
        if not high_risk_actions.issubset(interaction_ids):
            errors.append("route high-risk actions must reference declared page interactions")
        jobs: set[str] = set()
        states: set[str] = set()
        used_claims: set[str] = set()
        for unit in control:
            if not isinstance(unit, dict):
                continue
            copy_id = unit.get("copy_id")
            slot = page_slots.get(copy_id, {})
            jobs.add(unit.get("job"))
            states.add(unit.get("state"))
            claim_ids = set(unit.get("claim_ids") or [])
            used_claims.update(claim_ids)
            if slot and (unit.get("component_id") != slot.get("component_id") or unit.get("job") != slot.get("job") or unit.get("state") not in set(slot.get("states") or [])):
                errors.append("copy control must preserve each page slot component, job, and state contract")
            if unit.get("action_id") not in interaction_ids | {"none"}:
                errors.append("copy action_id must reference a page interaction or none")
            if unit.get("job") in {"promise", "prove"} and not claim_ids:
                errors.append("promise and proof copy must reference an approved claim")
            if PROHIBITED_COPY.search(str(unit.get("text") or "")):
                errors.append("copy control contains a mechanically prohibited persuasion pattern")
        if states != REQUIRED_PAGE_STATES:
            errors.append("copy control must cover every required page state")
        required_jobs = COPY_CHANNEL_JOBS.get(value.get("channel"), set())
        if not required_jobs.issubset(jobs):
            errors.append("copy control does not satisfy the route channel's required reader jobs")
        if high_risk_actions and "warn" not in jobs:
            errors.append("high-risk route copy must include an explicit warning unit")
        if expected_claims is not None and not used_claims.issubset(expected_claims):
            errors.append("copy control must reference declared claim IDs")
        if approved_claims is not None and not used_claims.issubset(approved_claims):
            errors.append("copy control may use only claims approved for copy")
        variants = value.get("variants") if isinstance(value.get("variants"), list) else []
        candidate_ids = [item.get("candidate_id") for item in variants if isinstance(item, dict)]
        mechanisms = [item.get("mechanism") for item in variants if isinstance(item, dict)]
        if not 3 <= len(variants) <= 5 or len(candidate_ids) != len(set(candidate_ids)) or len(mechanisms) != len(set(mechanisms)):
            errors.append("copy pack must include three to five unique mechanism-separated variants")
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            targets = set(variant.get("target_copy_ids") or [])
            replacements = {item.get("copy_id") for item in variant.get("replacements", []) if isinstance(item, dict)}
            if not targets or targets != replacements or not targets.issubset(set(control_ids)) or variant.get("performance_stage") not in COPY_STAGES:
                errors.append("copy variants must replace declared targets exactly and name a performance stage")
        truth = value.get("truth_agency_review") if isinstance(value.get("truth_agency_review"), dict) else {}
        if any(truth.get(field) is not True for field in ("claims_resolved", "material_terms_visible", "consequential_actions_clear", "reversibility_clear")) or truth.get("prohibited_patterns"):
            errors.append("copy truth and agency review must pass without prohibited patterns")
        methods = {item.get("method") for item in value.get("comprehension_checks", []) if isinstance(item, dict)}
        if not {"paraphrase", "expectation"}.issubset(methods):
            errors.append("copy pack must plan paraphrase and expectation checks before conversion testing")
    if kind == "copy-consistency":
        route_ids = set(value.get("route_ids") or [])
        pack_ids = set(value.get("copy_pack_ids") or [])
        coverage = value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
        if expected_routes is not None and (route_ids != expected_routes or pack_ids != expected_routes or set(coverage.get("route_ids") or []) != expected_routes):
            errors.append("copy consistency must cover every declared route and copy pack")
        if set(coverage.get("states") or []) != REQUIRED_PAGE_STATES:
            errors.append("copy consistency must cover every required page state")
        if expected_copy_ids is not None and set(coverage.get("copy_ids") or []) != expected_copy_ids:
            errors.append("copy consistency copy_ids must exactly match all selected controls")
        if expected_claims is not None and not set(coverage.get("claim_ids") or []).issubset(expected_claims):
            errors.append("copy consistency claim_ids must reference the claim ledger")
        referenced_copy_ids = {
            copy_id
            for item in value.get("promise_chain", []) if isinstance(item, dict)
            for copy_id in [item.get("entry_copy_id"), item.get("action_copy_id"), *(item.get("proof_copy_ids") or [])]
            if copy_id
        }
        referenced_copy_ids.update(copy_id for item in value.get("claim_usage", []) if isinstance(item, dict) for copy_id in item.get("copy_ids", []))
        referenced_copy_ids.update(item.get("copy_id") for item in value.get("voice_deviations", []) if isinstance(item, dict))
        if expected_copy_ids is not None and (not referenced_copy_ids.issubset(expected_copy_ids) or any(item.get("status") != "aligned" for item in value.get("promise_chain", []) if isinstance(item, dict))):
            errors.append("copy consistency references must resolve and every promise chain must align")
        if expected_claims is not None and not {item.get("claim_id") for item in value.get("claim_usage", []) if isinstance(item, dict)}.issubset(expected_claims):
            errors.append("copy consistency claim usage must reference the claim ledger")
        if (value.get("truth_agency_gate") or {}).get("status") != "pass" or (value.get("truth_agency_gate") or {}).get("violations"):
            errors.append("copy consistency truth and agency gate must pass")
    if kind == "copy-test-plan":
        if value.get("performance_claim_status") != "untested-candidates":
            errors.append("copy test plan must label all planned copy as untested candidates")
        tests = value.get("comprehension_tests") if isinstance(value.get("comprehension_tests"), list) else []
        tested_routes = {item.get("route_id") for item in tests if isinstance(item, dict)}
        if expected_routes is not None and tested_routes != expected_routes:
            errors.append("copy test plan must include comprehension coverage for every route")
        for experiment in value.get("experiment_candidates", []):
            if isinstance(experiment, dict) and experiment.get("route_id") not in (expected_routes or {experiment.get("route_id")}):
                errors.append("copy experiments must reference declared routes")
            if isinstance(experiment, dict) and experiment.get("one_mechanism") not in {"clarity", "reader-relevance", "mechanism-and-proof", "objection-or-risk", "autonomy-supportive", "narrative"}:
                errors.append("copy experiments must isolate one declared message mechanism")
            if isinstance(experiment, dict) and expected_variants is not None and experiment.get("treatment_candidate_id") not in expected_variants.get(experiment.get("route_id"), set()):
                errors.append("copy experiments must reference a declared route-level candidate")
    if kind == "copy-approval":
        if value.get("overall_status") != "approved-for-build" or value.get("performance_status") != "untested-candidates":
            errors.append("copy approval must distinguish build approval from untested performance")
        routes = value.get("routes") if isinstance(value.get("routes"), list) else []
        route_ids = {item.get("route_id") for item in routes if isinstance(item, dict)}
        if expected_routes is not None and route_ids != expected_routes:
            errors.append("copy approval must cover every route exactly once")
        scoped_units = [(item.get("route_id"), unit) for item in routes if isinstance(item, dict) for unit in item.get("controls", []) if isinstance(unit, dict)]
        units = [unit for _, unit in scoped_units]
        unit_ids = [f"{route_id}:{unit.get('copy_id')}" for route_id, unit in scoped_units]
        if expected_copy_ids is not None and (set(unit_ids) != expected_copy_ids or len(unit_ids) != len(set(unit_ids))):
            errors.append("copy approval must cover every route-scoped copy control exactly once")
        if expected_copy_units is not None:
            for route_id, unit in scoped_units:
                source = expected_copy_units.get(f"{route_id}:{unit.get('copy_id')}")
                if source is None:
                    continue
                for field in ("component_id", "location", "state", "job", "action_id", "character_limit", "accessibility", "localization"):
                    if unit.get(field) != source.get(field):
                        errors.append("approved copy must preserve controller-owned page and interaction metadata")
                        break
                text = unit.get("text")
                if not isinstance(text, str) or not text.strip():
                    errors.append(f"approved copy {route_id}:{unit.get('copy_id')} must be non-empty")
                elif len(text) > unit.get("character_limit", 0):
                    errors.append(f"approved copy {route_id}:{unit.get('copy_id')} is {len(text)}/{unit.get('character_limit')} characters")
                elif PROHIBITED_COPY.search(text):
                    errors.append(f"approved copy {route_id}:{unit.get('copy_id')} contains prohibited persuasion")
                if approved_claims is not None and not set(unit.get("claim_ids") or []).issubset(approved_claims):
                    errors.append("approved copy may reference only claims approved for copy")
        resolutions = value.get("issue_resolutions") if isinstance(value.get("issue_resolutions"), list) else []
        resolution_ids = [item.get("issue_id") for item in resolutions if isinstance(item, dict)]
        if expected_issue_ids is not None and (set(resolution_ids) != expected_issue_ids or len(resolution_ids) != len(set(resolution_ids))):
            errors.append("copy approval must resolve every critical and high consistency issue exactly once")
        if not value.get("source_of_truth"):
            errors.append("copy approval must declare source-of-truth precedence")
    if kind == "asset-plan":
        assets = value.get("assets") if isinstance(value.get("assets"), list) else []
        asset_ids = [item.get("asset_id") for item in assets if isinstance(item, dict)]
        if not asset_ids or len(asset_ids) != len(set(asset_ids)) or any(not isinstance(item, str) or not SAFE_ID.fullmatch(item) for item in asset_ids):
            errors.append("asset IDs must be non-empty, unique, and safe")
        route_references = {placement.get("route_id") for asset in assets if isinstance(asset, dict) for placement in asset.get("placements", []) if isinstance(placement, dict)}
        if expected_routes is not None and not route_references.issubset(expected_routes):
            errors.append("asset placements must reference declared routes")
        if (value.get("generation_policy") or {}).get("model") != "gpt-image-2":
            errors.append("generated asset policy must pin gpt-image-2")
        for asset in assets:
            production = asset.get("production") if isinstance(asset, dict) else {}
            if not isinstance(production, dict) or not production.get("formats") or not production.get("alt_text_rule"):
                errors.append("every asset must define output formats and an alt-text rule")
                break
            if production.get("method") == "generate" and production.get("model") != "gpt-image-2":
                errors.append("generated assets must use gpt-image-2")
                break
    if kind == "architecture":
        names = {str(item.get("name") or "").lower() for item in value.get("services", []) if isinstance(item, dict)}
        payments = value.get("payments") if isinstance(value.get("payments"), dict) else {}
        images = value.get("images") if isinstance(value.get("images"), dict) else {}
        ai = value.get("ai") if isinstance(value.get("ai"), dict) else {}
        decisions = {
            "Cloudflare Workers": any("worker" in name for name in names),
            "Cloudflare AI Gateway": any("ai-gateway" in name or "ai gateway" in name for name in names) or "ai gateway" in str(ai.get("gateway") or "").lower(),
            "Clerk": any("clerk" in name for name in names),
            "Stripe": any("stripe" in name for name in names) or all(payments.get(field) is not None for field in ("flow", "entitlement_source", "idempotency")),
            "OpenAI GPT Image 2": any("gpt image" in name or "gpt-image" in name for name in names) or images.get("model") == "gpt-image-2",
        }
        for required_service, decided in decisions.items():
            if not decided:
                errors.append(f"architecture must decide use/boundary for {required_service}")
    if kind == "final-plan" and expected_routes is not None:
        route_ids = value.get("route_ids")
        page_ids = value.get("page_ids")
        if set(route_ids or []) != expected_routes or set(page_ids or []) != expected_routes:
            errors.append("final route_ids and page_ids must exactly match the route manifest")
        if "unresolved" in value or value.get("open_questions"):
            errors.append("final plan cannot delegate decisions as unresolved questions; choose a conservative reversible default")
        closures = value.get("decision_closure") if isinstance(value.get("decision_closure"), list) else []
        for item in closures:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not SAFE_ID.fullmatch(item["id"])
                or item.get("evidence_status") not in {"supplied", "inference", "assumption"}
                or item.get("reversible") is not True
                or any(not isinstance(item.get(field), str) or not item[field].strip() for field in ("uncertainty", "decision", "rationale", "risk", "fallback", "revisit_trigger"))
            ):
                errors.append("every final decision closure must choose a reversible default with rationale, risk, fallback, and revisit trigger")
                break
        external = value.get("external_verification") if isinstance(value.get("external_verification"), list) else []
        for item in external:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not SAFE_ID.fullmatch(item["id"])
                or item.get("status") not in {"required", "verified", "not-required"}
                or item.get("blocking_for") not in {"launch", "production-use"}
                or any(not isinstance(item.get(field), str) or not item[field].strip() for field in ("fact", "reason_unverifiable", "method", "safe_fallback", "evidence"))
            ):
                errors.append("external verification must name an unknowable fact, exact method, launch-only boundary, and safe fallback")
                break
        if value.get("implementation_ready") is not True:
            errors.append("final implementation_ready must be true; the planner must resolve every product decision itself")
        launch_ready = value.get("launch_ready")
        required_external = any(isinstance(item, dict) and item.get("status") == "required" for item in external)
        if not isinstance(launch_ready, bool) or launch_ready != (not required_external):
            errors.append("final launch_ready must equal whether required external verification is absent")
        if expected_assets is not None and set(value.get("asset_ids") or []) != expected_assets:
            errors.append("final asset_ids must exactly match the asset plan")
        if set(value.get("copy_pack_ids") or []) != expected_routes:
            errors.append("final copy_pack_ids must exactly match the route manifest")
        if expected_claims is not None and set(value.get("claim_ids") or []) != expected_claims:
            errors.append("final claim_ids must exactly match the claim ledger")
        if expected_copy_ids is not None and set(value.get("copy_ids") or []) != expected_copy_ids:
            errors.append("final copy_ids must exactly match the selected copy controls")
        if value.get("copy_test_status") != "untested-candidates":
            errors.append("final copy test status must preserve the untested-candidate boundary")
        if value.get("copy_approval_status") != "approved-for-build":
            errors.append("final plan must bind the approved-for-build copy package")
        if expected_issue_ids is not None and set(value.get("resolved_issue_ids") or []) != expected_issue_ids:
            errors.append("final plan must carry every resolved critical and high consistency issue")
        if not value.get("source_of_truth"):
            errors.append("final plan must declare artifact source-of-truth precedence")
    return errors
