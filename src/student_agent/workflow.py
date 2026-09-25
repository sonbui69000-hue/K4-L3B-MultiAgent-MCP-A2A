from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

RETRYABLE_ERROR_MARKERS = ("timeout", "timed out", "temporarily unavailable", "503")
REFUND_ISSUES = {"canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed"}


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return [value] if isinstance(value, dict) else []


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _number(value: Decimal) -> int | float:
    value = value.quantize(Decimal("0.01"))
    return int(value) if value == value.to_integral() else float(value)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _first_order_id(data: Any) -> str | None:
    rows = _records(data)
    return rows[0].get("order_id") if rows and isinstance(rows[0].get("order_id"), str) else None


async def _call(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
) -> dict[str, Any] | None:
    for attempt in range(3):
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence["evidence_ref"]],
            )
            return evidence
        except Exception as exc:
            message = str(exc).lower()
            if attempt < 2 and any(marker in message for marker in RETRYABLE_ERROR_MARKERS):
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            return None
    return None


def _shipment_verdict(shipment: dict[str, Any], order: dict[str, Any]) -> str:
    events = _records(shipment.get("events"))
    for event in events:
        event_type = str(event.get("event_type", "")).lower()
        if event_type in {"lost", "returned"}:
            return event_type
        if event_type == "delivered_late":
            actor = str(event.get("actor", "")).lower()
            return "seller_delay" if actor == "seller" else "logistics_delay"

    delivered = _time(shipment.get("delivered_customer_at") or order.get("order_delivered_customer_date"))
    estimated = _time(shipment.get("estimated_delivery_at") or order.get("order_estimated_delivery_date"))
    if delivered and estimated:
        return "on_time" if delivered <= estimated else "logistics_delay"
    return "insufficient_evidence"


def _payment_totals(payment: dict[str, Any], timeline: dict[str, Any] | None) -> tuple[Decimal, Decimal]:
    rows = _records(payment.get("payments"))
    captured = sum((_money(row.get("payment_value")) for row in rows), Decimal("0"))
    if timeline:
        events = _records(timeline.get("events"))
        captured_events = [
            _money(event.get("amount_brl"))
            for event in events
            if str(event.get("event_type", "")).lower() == "captured"
        ]
        if captured_events:
            captured = sum(captured_events, Decimal("0"))
    refunded = Decimal("0")
    if timeline:
        for event in _records(timeline.get("events")):
            if str(event.get("event_type", "")).lower() in {"refunded", "refund_succeeded"}:
                refunded += _money(event.get("amount_brl"))
    return captured, refunded


def _claim_verdict(topic: str, primary_issue: str, status: str) -> str:
    if topic == "requested_full_refund":
        return "supported" if status == "action_required" else "insufficient_evidence"
    if topic == primary_issue:
        return "supported" if primary_issue != "unsupported_claim" else "unsupported"
    return "partially_supported" if status != "needs_investigation" else "insufficient_evidence"


EXPECTED_PARTIES = {
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "seller",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "payment_mismatch": "payment_provider",
    "duplicate_charge": "payment_provider",
    "refund_pending": "payment_provider",
    "refund_failed": "payment_provider",
    "valid_split_payment": "customer",
    "unsupported_claim": "customer",
}


def _relevant_refs(
    primary_issue: str,
    *,
    order_ref: str | None,
    item_ref: str | None,
    payment_ref: str | None,
    payment_timeline_ref: str | None,
    shipment_ref: str | None,
    product_ref: str | None,
    seller_ref: str | None,
    policy_ref: str | None,
    refund_ref: str | None,
) -> list[str]:
    refs: list[str] = []
    def add(*values: str | None) -> None:
        refs.extend(value for value in values if value)

    add(policy_ref)
    if primary_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        add(shipment_ref, item_ref, seller_ref)
    elif primary_issue in {"payment_mismatch", "duplicate_charge", "valid_split_payment"}:
        add(payment_ref, payment_timeline_ref)
    elif primary_issue in REFUND_ISSUES:
        add(order_ref, payment_ref, payment_timeline_ref, refund_ref)
    elif primary_issue in {"canceled_order_paid", "unavailable_order_paid"}:
        add(order_ref, item_ref, payment_ref)
    else:
        add(order_ref, item_ref)
    if primary_issue == "requested_full_refund":
        add(refund_ref, payment_ref)
    return list(dict.fromkeys(refs))


def _arbitrate(
    *,
    primary_issue: str,
    policy_rule: dict[str, Any],
    shipment_verdict: str,
    payment_verdict: str,
    evidence_present: dict[str, bool],
    entity_status: str,
) -> tuple[str, Decimal, list[dict[str, Any]], str | None, list[dict[str, Any]], float]:
    conflicts: list[dict[str, Any]] = []
    expected_party = EXPECTED_PARTIES.get(primary_issue)
    policy_parties = policy_rule.get("responsible_parties", [])
    policy_party_types = {
        str(item.get("party_type"))
        for item in policy_parties
        if isinstance(item, dict) and item.get("party_type")
    }
    if expected_party and policy_party_types and expected_party not in policy_party_types:
        conflicts.append({
            "field": "root_cause_analysis.responsible_parties",
            "sources": ["case_claim", "policy"],
            "selected_source": None,
            "resolution_code": "responsibility_conflict",
        })

    if primary_issue == "late_delivery_seller" and shipment_verdict != "seller_delay":
        conflicts.append({
            "field": "shipment_analysis.verdict",
            "sources": ["shipment", "case_claim"],
            "selected_source": "shipment",
            "resolution_code": "claim_not_supported_by_timeline",
        })
    if primary_issue == "late_delivery_logistics" and shipment_verdict != "logistics_delay":
        conflicts.append({
            "field": "shipment_analysis.verdict",
            "sources": ["shipment", "case_claim"],
            "selected_source": "shipment",
            "resolution_code": "claim_not_supported_by_timeline",
        })
    if primary_issue == "refund_pending" and payment_verdict != "refund_pending":
        conflicts.append({
            "field": "payment_analysis.verdict",
            "sources": ["refund", "payment"],
            "selected_source": "payment",
            "resolution_code": "refund_state_conflict",
        })
    if primary_issue == "refund_failed" and payment_verdict != "refund_failed":
        conflicts.append({
            "field": "payment_analysis.verdict",
            "sources": ["refund", "payment"],
            "selected_source": "payment",
            "resolution_code": "refund_state_conflict",
        })

    status = str(policy_rule.get("case_status", "needs_investigation"))
    amount = _money(policy_rule.get("refund_brl"))
    action = policy_rule.get("recommended_action")
    responsible = policy_parties if isinstance(policy_parties, list) else []
    if conflicts or not policy_rule:
        status = "needs_investigation"
        action = None
        amount = Decimal("0")

    required = {"order": True, "policy": True}
    missing = sum(
        1 for name, required_flag in required.items()
        if required_flag and not evidence_present.get(name, False)
    )
    missing += sum(
        1 for name in ("items", "payment", "shipment")
        if not evidence_present.get(name, False)
    )
    confidence = 0.95
    confidence -= min(0.30, missing * 0.10)
    if entity_status != "resolved":
        confidence -= 0.20
    if conflicts:
        confidence -= min(0.35, 0.15 * len(conflicts))
    if status == "needs_investigation":
        confidence = min(confidence, 0.55)
    confidence = max(0.10, min(0.95, round(confidence, 2)))
    return status, amount, responsible[:5], action, conflicts[:5], confidence


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    request = case.get("customer_request") or {}
    claims = [claim for claim in request.get("claims", []) if isinstance(claim, dict)]
    primary_issue = str(claims[0].get("topic", "insufficient_evidence")) if claims else "insufficient_evidence"
    candidate_ids = [value for value in case.get("candidate_order_ids", []) if isinstance(value, str)]
    claimed_order_id = request.get("claimed_order_id")
    if not isinstance(claimed_order_id, str) and candidate_ids:
        claimed_order_id = candidate_ids[0]

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="resolve_entity",
    )
    order_evidence = None
    if claimed_order_id:
        order_evidence = await _call(
            gateway,
            trace,
            case_id=case_id,
            actor="entity-agent",
            tool_name="get_order",
            arguments={"order_id": claimed_order_id},
        )

    order_data = order_evidence.get("data", {}) if order_evidence else {}
    resolved_order_id = order_data.get("order_id") if isinstance(order_data, dict) else None
    entity_status = "resolved" if resolved_order_id == claimed_order_id else (
        "ambiguous" if candidate_ids else "not_found"
    )
    resolved_ids = [resolved_order_id] if isinstance(resolved_order_id, str) else []
    rejected_ids = [candidate for candidate in candidate_ids if candidate not in resolved_ids]
    if order_evidence:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="entity-agent",
            target="coordinator",
            decision_code="entity_resolved",
        )

    if not resolved_order_id:
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="entity_unresolved",
        )
        return _minimal_output(
            case,
            primary_issue=primary_issue,
            entity_status=entity_status,
            resolved_ids=resolved_ids,
            rejected_ids=rejected_ids,
        )

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="specialists",
        decision_code="parallel_investigation",
    )
    order_args = {"order_id": resolved_order_id}
    scope = case.get("investigation_scope") or {}
    customer_evidence = None
    if scope.get("include_customer_history") and isinstance(case.get("customer_unique_id_hint"), str):
        customer_evidence = await _call(
            gateway, trace, case_id=case_id, actor="entity-agent",
            tool_name="get_customer_history",
            arguments={"customer_unique_id": case["customer_unique_id_hint"]},
        )

    item_evidence = await _call(
        gateway, trace, case_id=case_id, actor="order-agent",
        tool_name="get_order_items", arguments=order_args,
    )
    payment_evidence = await _call(
        gateway, trace, case_id=case_id, actor="payment-agent",
        tool_name="get_order_payments", arguments=order_args,
    )
    payment_timeline_evidence = await _call(
        gateway, trace, case_id=case_id, actor="payment-agent",
        tool_name="get_payment_timeline", arguments=order_args,
    )
    shipment_evidence = await _call(
        gateway, trace, case_id=case_id, actor="shipment-agent",
        tool_name="get_shipment_summary", arguments=order_args,
    )
    product_evidence = None
    seller_evidence = None
    if scope.get("include_product_context"):
        product_evidence = await _call(
            gateway, trace, case_id=case_id, actor="order-agent",
            tool_name="get_product_context", arguments=order_args,
        )
        seller_evidence = await _call(
            gateway, trace, case_id=case_id, actor="order-agent",
            tool_name="get_sellers", arguments=order_args,
        )
    policy_evidence = await _call(
        gateway, trace, case_id=case_id, actor="policy-agent",
        tool_name="get_policy", arguments={"policy_version": str(case.get("policy_version", ""))},
    )
    refund_evidence = None
    if primary_issue in REFUND_ISSUES or primary_issue in {"payment_mismatch", "duplicate_charge"}:
        refund_evidence = await _call(
            gateway, trace, case_id=case_id, actor="payment-agent",
            tool_name="get_refund_timeline", arguments=order_args,
        )

    items = item_evidence.get("data", []) if item_evidence else []
    items = _records(items)
    payments = payment_evidence.get("data", []) if payment_evidence else []
    payment_data = {"payments": payments}
    timeline_data = payment_timeline_evidence.get("data", {}) if payment_timeline_evidence else None
    captured, refunded = _payment_totals(payment_data, timeline_data)
    shipment_data = shipment_evidence.get("data", {}) if shipment_evidence else {}
    shipment_verdict = _shipment_verdict(shipment_data, order_data)
    policy_data = policy_evidence.get("data", {}) if policy_evidence else {}
    rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    policy_rule = rules.get(primary_issue, {}) if isinstance(rules, dict) else {}
    case_status = policy_rule.get("case_status", "needs_investigation")
    if not policy_evidence:
        case_status = "needs_investigation"

    evidence_refs = [
        evidence["evidence_ref"]
        for evidence in (
            order_evidence, customer_evidence, item_evidence, payment_evidence,
            payment_timeline_evidence, shipment_evidence, product_evidence,
            seller_evidence, policy_evidence, refund_evidence,
        )
        if evidence
    ]
    item_ids = list(dict.fromkeys(
        str(item.get("order_item_id")) for item in items if item.get("order_item_id") is not None
    ))
    seller_ids = list(dict.fromkeys(
        str(item.get("seller_id")) for item in items if item.get("seller_id") is not None
    ))
    product_ids = list(dict.fromkeys(
        str(item.get("product_id")) for item in items if item.get("product_id") is not None
    ))
    payment_refs = [
        f"{resolved_order_id}:payment:{index}"
        for index, payment in enumerate(payments, start=1)
        if isinstance(payment, dict)
    ]
    customer_data = customer_evidence.get("data", {}) if customer_evidence else {}
    related_orders = [
        str(row["order_id"]) for row in _records(customer_data.get("orders"))
        if row.get("order_id") is not None
    ]
    refund_data = refund_evidence.get("data", {}) if refund_evidence else {}
    refund_events = _records(refund_data.get("events"))
    refund_statuses = {str(event.get("status", "")).lower() for event in refund_events}
    if "failed" in refund_statuses:
        payment_verdict = "refund_failed"
    elif "pending" in refund_statuses:
        payment_verdict = "refund_pending"
    elif refunded > 0:
        payment_verdict = "refunded"
    elif primary_issue == "duplicate_charge":
        payment_verdict = "duplicate_capture"
    elif primary_issue == "payment_mismatch":
        payment_verdict = "capture_mismatch"
    elif payment_timeline_evidence:
        payment_verdict = "reconciled"
    else:
        payment_verdict = "insufficient_evidence"

    evidence_by_tool = {
        "order": order_evidence["evidence_ref"] if order_evidence else None,
        "items": item_evidence["evidence_ref"] if item_evidence else None,
        "payment": payment_evidence["evidence_ref"] if payment_evidence else None,
        "payment_timeline": (
            payment_timeline_evidence["evidence_ref"]
            if payment_timeline_evidence else None
        ),
        "shipment": shipment_evidence["evidence_ref"] if shipment_evidence else None,
        "product": product_evidence["evidence_ref"] if product_evidence else None,
        "seller": seller_evidence["evidence_ref"] if seller_evidence else None,
        "policy": policy_evidence["evidence_ref"] if policy_evidence else None,
        "refund": refund_evidence["evidence_ref"] if refund_evidence else None,
    }
    case_status, financial_amount, responsible, recommended_action, data_conflicts, confidence = _arbitrate(
        primary_issue=primary_issue,
        policy_rule=policy_rule,
        shipment_verdict=shipment_verdict,
        payment_verdict=payment_verdict,
        evidence_present={name: value is not None for name, value in evidence_by_tool.items()},
        entity_status=entity_status,
    )
    relevant_refs = _relevant_refs(
        primary_issue,
        order_ref=evidence_by_tool["order"],
        item_ref=evidence_by_tool["items"],
        payment_ref=evidence_by_tool["payment"],
        payment_timeline_ref=evidence_by_tool["payment_timeline"],
        shipment_ref=evidence_by_tool["shipment"],
        product_ref=evidence_by_tool["product"],
        seller_ref=evidence_by_tool["seller"],
        policy_ref=evidence_by_tool["policy"],
        refund_ref=evidence_by_tool["refund"],
    )
    financial_amount = max(Decimal("0"), financial_amount)
    refund_lines = []
    if financial_amount > 0 and recommended_action:
        refund_lines.append({
            "reason_code": str(recommended_action),
            "amount_brl": _number(financial_amount),
            "entity_id": resolved_order_id,
        })
    ranked_cause = {
        "cause_code": primary_issue.upper().replace("-", "_"),
        "rank": 1,
    }
    claim_assessments = [
        {
            "claim_id": str(claim.get("claim_id", f"claim-{index}")),
            "verdict": _claim_verdict(str(claim.get("topic", "")), primary_issue, case_status),
            "confidence": confidence,
            "evidence_refs": relevant_refs[:8],
        }
        for index, claim in enumerate(claims, start=1)
    ]

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="specialists",
        target="conflict-resolver",
        decision_code="evidence_assembled",
        evidence_refs=evidence_refs[:20],
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=str(policy_rule.get("recommended_action", "no_policy_decision")),
        evidence_refs=[policy_evidence["evidence_ref"]] if policy_evidence else None,
    )

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [
                str(claim["topic"]) for claim in claims[1:]
                if claim.get("topic")
            ],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": resolved_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": [f"shipment:{resolved_order_id}"],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved_ids,
            "rejected_candidates": rejected_ids,
            "confidence": confidence,
        },
        "customer_context": {
            "customer_unique_id": (
                customer_data.get("customer_unique_id")
                if isinstance(customer_data, dict) else None
            ),
            "related_order_ids": list(dict.fromkeys(related_orders)),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": seller_ids if shipment_verdict == "seller_delay" else [],
            "timeline_complete": bool(shipment_evidence and shipment_data.get("events")),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": _number(captured) if payment_evidence else None,
            "refunded_total_brl": _number(refunded) if payment_timeline_evidence else None,
            "refundable_total_brl": _number(financial_amount) if policy_evidence else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [ranked_cause],
            "responsible_parties": responsible[:5],
        },
        "evidence_refs": list(dict.fromkeys(evidence_refs))[:30],
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _number(financial_amount),
            "refund_lines": refund_lines,
        },
        "resolution_actions": (
            [str(recommended_action)]
            if recommended_action else []
        ),
    }
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="schema_and_scope_checked",
        evidence_refs=evidence_refs[:20],
    )
    return output


def _minimal_output(
    case: dict[str, Any],
    *,
    primary_issue: str,
    entity_status: str,
    resolved_ids: list[str],
    rejected_ids: list[str],
) -> dict[str, Any]:
    claims = case.get("customer_request", {}).get("claims", [])
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [str(c.get("topic")) for c in claims[1:] if isinstance(c, dict)],
            "case_status": "needs_investigation",
            "confidence": 0.1,
        },
        "affected_entities": {
            "order_ids": resolved_ids,
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [],
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved_ids,
            "rejected_candidates": rejected_ids,
            "confidence": 0.1,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }
