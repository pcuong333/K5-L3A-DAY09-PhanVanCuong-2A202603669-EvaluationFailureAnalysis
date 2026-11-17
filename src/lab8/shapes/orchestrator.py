"""Shape C — orchestrator plus read-only investigators.

1. Build a handoff with `lab8.handoff.make_handoff`. Allowed tools are read-only.
   Never put parent_secret, raw headers, or the full claim row into facts.
2. `lab8.runtime.run_readonly_investigators` runs those tools.
3. The orchestrator quotes from the investigator's order view and calls
   `durability.issue_refund`. Investigators have no write registry.
   Pass the full quote; the host subtracts earlier refunds on the same order.
   `lookup_refund_history` is a read-only tool you may hand to an investigator.
4. Trace the sanitized handoff, not the parent context.

An LLM summary of the parent context is not a sanitizer. Use the allowlist.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations

from lab8.db import connect
from lab8.durability import issue_refund
from lab8.handoff import make_handoff
from lab8.runtime import run_readonly_investigators
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish, safe_dump
from lab8.tools import get_claim_view, tool_cost
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        budget = Budget()
        trace = Trace(make_run_id("orchestrator", claim_id, seed))

        budget.add_tool(tool_cost())
        claim = get_claim_view(conn, claim_id)
        trace.add(
            "claim",
            {"found": claim["found"], "has_order_id": bool(claim.get("order_id"))},
        )
        if not claim["found"]:
            return finish(
                conn,
                shape="orchestrator",
                claim_id=claim_id,
                seed=seed,
                status="failed",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason="unknown_claim",
            )
        if not claim.get("order_id"):
            return finish(
                conn,
                shape="orchestrator",
                claim_id=claim_id,
                seed=seed,
                status="needs_info",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason="missing_order_id",
            )

        handoff = make_handoff(
            request_id=f"REQ-{claim_id}",
            claim_id=claim_id,
            task="check_applicable_policy",
            facts={},
            allowed_tools=[
                "lookup_order",
                "lookup_policy",
                "compute_compensation",
            ],
        )
        trace.add("handoff", safe_dump(handoff))

        investigated = run_readonly_investigators(
            conn,
            budget,
            handoff,
            principal_id=claim["principal_id"],
        )
        tools = investigated["tools"]
        order_view = tools["lookup_order"]
        policies = tools["lookup_policy"]["policies"]
        trace.add(
            "investigator",
            {
                "lookup_order": order_view,
                "policy_count": len(policies),
            },
        )
        if not order_view["visible"]:
            return finish(
                conn,
                shape="orchestrator",
                claim_id=claim_id,
                seed=seed,
                status="escalated",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason=order_view["reason"],
            )

        quote = tools.get("compute_compensation")
        if quote is None:
            trace.add("error", {"reason": "missing_compensation_result"})
            return finish(
                conn,
                shape="orchestrator",
                claim_id=claim_id,
                seed=seed,
                status="failed",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason="missing_compensation_result",
            )

        proposal = int(quote["amount_vnd"])
        trace.add("quote", quote)
        if proposal <= 0:
            return finish(
                conn,
                shape="orchestrator",
                claim_id=claim_id,
                seed=seed,
                status="denied",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason=quote["reason"],
            )

        trace.add("refund_request", {"proposal_amount": proposal})
        trace.write()
        outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
        trace.add(
            "refund_result",
            {
                "ok": outcome["ok"],
                "reason": outcome["reason"],
                "conflict": outcome["conflict"],
            },
        )
        if not outcome["ok"]:
            return finish(
                conn,
                shape="orchestrator",
                claim_id=claim_id,
                seed=seed,
                status="failed" if outcome["conflict"] else "denied",
                proposal_amount=proposal,
                budget=budget,
                trace=trace,
                error_reason=outcome["reason"],
            )
        return finish(
            conn,
            shape="orchestrator",
            claim_id=claim_id,
            seed=seed,
            status="resolved",
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
        )
    finally:
        conn.close()
