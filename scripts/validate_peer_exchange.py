#!/usr/bin/env python3
"""Validate one bounded peer exchange and write a metadata-only receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
SAFE_STAGE = re.compile(r"^[a-z][a-z0-9-]*$")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_exact_keys(value: Any, required: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unsupported " + ", ".join(unknown))
        raise ValueError(f"{field} fields invalid: {'; '.join(details)}")
    return value


def require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} must be a safe non-empty identifier")
    return value


def require_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    return value


def validate_finding(value: Any, index: int) -> None:
    field = f"response.findings[{index}]"
    finding = require_exact_keys(value, {"id", "severity", "claim", "evidence"}, field)
    require_id(finding["id"], f"{field}.id")
    if finding["severity"] not in {"info", "low", "medium", "high", "critical"}:
        raise ValueError(f"{field}.severity is invalid")
    if not isinstance(finding["claim"], str) or not finding["claim"].strip():
        raise ValueError(f"{field}.claim must be non-empty")
    if not isinstance(finding["evidence"], list):
        raise ValueError(f"{field}.evidence must be an array")
    for evidence_index, item in enumerate(finding["evidence"], start=1):
        evidence_field = f"{field}.evidence[{evidence_index}]"
        if not isinstance(item, dict) or not {"source", "locator"}.issubset(item) or set(item) - {"source", "locator", "digest"}:
            raise ValueError(f"{evidence_field} must contain source, locator, and optional digest")
        for name in ("source", "locator"):
            if not isinstance(item[name], str) or not item[name].strip():
                raise ValueError(f"{evidence_field}.{name} must be non-empty")
        if "digest" in item and (not isinstance(item["digest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"])):
            raise ValueError(f"{evidence_field}.digest must be sha256:<64 lowercase hex>")


def validate_exchange(contract: dict[str, Any], request: dict[str, Any], response: dict[str, Any], response_size: int) -> dict[str, Any]:
    request = require_exact_keys(
        request,
        {"schema_version", "message_id", "correlation_id", "stage", "sender", "recipient", "hop", "sent_at", "payload"},
        "request",
    )
    response = require_exact_keys(
        response,
        {"schema_version", "message_id", "correlation_id", "in_reply_to", "stage", "sender", "recipient", "hop", "completed_at", "status", "summary", "findings", "error"},
        "response",
    )
    if request["schema_version"] != "1.0" or response["schema_version"] != "1.0":
        raise ValueError("request and response schema_version must be 1.0")
    for field in ("message_id", "correlation_id", "sender", "recipient"):
        require_id(request[field], f"request.{field}")
    for field in ("message_id", "correlation_id", "in_reply_to", "sender", "recipient"):
        require_id(response[field], f"response.{field}")
    require_timestamp(request["sent_at"], "request.sent_at")
    require_timestamp(response["completed_at"], "response.completed_at")
    if not isinstance(request["stage"], str) or not SAFE_STAGE.fullmatch(request["stage"]):
        raise ValueError("request.stage must be safe kebab-case")
    if request["stage"] not in contract.get("stages", []):
        raise ValueError(f"request.stage is not peer-enabled: {request['stage']}")
    if response["stage"] != request["stage"]:
        raise ValueError("response.stage must match request.stage")
    peer_ids = {peer.get("id") for peer in contract.get("peers", []) if isinstance(peer, dict)}
    if request["recipient"] not in peer_ids:
        raise ValueError("request.recipient is not a declared peer")
    if request["sender"] != "controller" and request["sender"] not in peer_ids:
        raise ValueError("request.sender is not controller or a declared peer")
    if response["sender"] != request["recipient"] or response["recipient"] != request["sender"]:
        raise ValueError("response sender and recipient must reverse the request route")
    if response["correlation_id"] != request["correlation_id"] or response["in_reply_to"] != request["message_id"]:
        raise ValueError("response correlation does not match the request")
    if response["message_id"] == request["message_id"]:
        raise ValueError("response.message_id must differ from request.message_id")
    max_hops = int(contract["limits"]["max_hops"])
    for value, field in ((request["hop"], "request.hop"), (response["hop"], "response.hop")):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= max_hops:
            raise ValueError(f"{field} must be between 0 and {max_hops}")
    if response["hop"] != request["hop"]:
        raise ValueError("response.hop must match request.hop")
    if not isinstance(request["payload"], dict):
        raise ValueError("request.payload must be an object")
    max_response_bytes = int(contract["limits"]["max_response_bytes"])
    if response_size > max_response_bytes:
        raise ValueError(f"response exceeds max_response_bytes ({response_size} > {max_response_bytes})")
    if response["status"] not in {"completed", "insufficient_evidence", "error"}:
        raise ValueError("response.status is invalid")
    if not isinstance(response["summary"], str):
        raise ValueError("response.summary must be text")
    if not isinstance(response["findings"], list) or len(response["findings"]) > 100:
        raise ValueError("response.findings must contain at most 100 findings")
    for index, finding in enumerate(response["findings"], start=1):
        validate_finding(finding, index)
    finding_ids = [finding["id"] for finding in response["findings"]]
    if len(finding_ids) != len(set(finding_ids)):
        raise ValueError("response finding ids must be unique")
    if response["status"] == "error":
        if not isinstance(response["error"], str) or not response["error"].strip():
            raise ValueError("error responses require a non-empty error")
        if response["findings"]:
            raise ValueError("error responses cannot include findings")
    elif response["error"] is not None:
        raise ValueError("non-error responses require error=null")
    if response["status"] == "completed" and not response["summary"].strip():
        raise ValueError("completed responses require a non-empty summary")
    return {
        "schema_version": "1.0",
        "message_id": request["message_id"],
        "response_message_id": response["message_id"],
        "correlation_id": request["correlation_id"],
        "stage": request["stage"],
        "sender": request["sender"],
        "recipient": request["recipient"],
        "hop": request["hop"],
        "status": response["status"],
        "finding_count": len(response["findings"]),
    }


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    contract_path = Path(args.contract).expanduser().resolve()
    request_path = Path(args.request).expanduser().resolve()
    response_path = Path(args.response).expanduser().resolve()
    response_bytes = response_path.read_bytes()
    try:
        receipt = validate_exchange(read_json(contract_path), read_json(request_path), json.loads(response_bytes), len(response_bytes))
    except (ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    receipt["contract_sha256"] = digest_bytes(contract_path.read_bytes())
    receipt["request_sha256"] = digest_bytes(request_path.read_bytes())
    receipt["response_sha256"] = digest_bytes(response_bytes)
    output = Path(args.receipt).expanduser().resolve()
    atomic_write(output, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
