"""Shape B — one bounded ReAct agent.

Use `lab8.runtime.run_react` with max_steps=8. `decide` may pick a read tool.
It must not call `issue_refund`. After the loop, the host validates the quote
and is the only writer.

A decide() that trusts ticket text over the order row will fail hidden claims
where the customer overstates the delay or cites a superseded clause.

Per-order cap: an order may already have been refunded by an earlier claim.
The read tool `lookup_refund_history` shows how much. The host still enforces
the cap, so pass the full quote to `durability.issue_refund`, not a net amount.

Caps: stop at 8 steps. Count model calls in the Budget so cost stays comparable.

Trace contract v1 (docs/contracts.md): return a trace_path to a JSON file written in this run,
{"run_id": <same run_id>, "events": [...]}, on every return path including early
ones. `lab8.trace.Trace` does this; your own tracer is fine if it matches.
"""

from __future__ import annotations

from typing import Any

from lab8.db import connect
from lab8.durability import issue_refund
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
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
        trace = Trace(make_run_id("single", claim_id, seed))
        budget.add_tool(tool_cost())
        claim = get_claim_view(conn, claim_id)
        trace.add(
            "claim",
            {"found": claim["found"], "has_order_id": bool(claim.get("order_id"))},
        )
        if not claim["found"]:
            return finish(
                conn,
                shape="single",
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
                shape="single",
                claim_id=claim_id,
                seed=seed,
                status="needs_info",
                proposal_amount=0,
                budget=budget,
                trace=trace,
                error_reason="missing_order_id",
            )

        claim_context = {
            "claim_id": claim_id,
            "principal_id": claim["principal_id"],
            "order_id": claim["order_id"],
        }

        def decide(seen: dict[str, Any]) -> dict[str, Any]:
            last_tool = seen.get("last_tool")
            phase = str(last_tool or "start")
            complete(
                budget,
                seed=seed,
                purpose="plan",
                prompt=f"claim={claim_id}; phase={phase}",
            )
            trace.add("react_step", {"phase": phase})

            if last_tool == "lookup_order":
                order_view = seen["last_result"]
                if not order_view["visible"]:
                    trace.add(
                        "lookup_order",
                        {"visible": False, "reason": order_view["reason"]},
                    )
                    return {
                        "final": {
                            "status": "escalated",
                            "proposal_amount": 0,
                            "error_reason": order_view["reason"],
                        }
                    }
                order = order_view["order"]
                trace.add(
                    "lookup_order",
                    {"visible": True, "order_id": order["order_id"]},
                )
                return {
                    "tool": "compute_compensation",
                    "args": {"order": order},
                }

            if last_tool == "compute_compensation":
                quote = seen["last_result"]
                proposal = int(quote["amount_vnd"])
                trace.add("quote", quote)
                return {
                    "final": {
                        "status": "quoted" if proposal > 0 else "denied",
                        "proposal_amount": proposal,
                        "error_reason": quote["reason"],
                    }
                }

            if last_tool is None:
                return {
                    "tool": "lookup_order",
                    "args": {
                        "order_id": claim_context["order_id"],
                        "principal_id": claim_context["principal_id"],
                    },
                }

            return {
                "final": {
                    "status": "failed",
                    "proposal_amount": 0,
                    "error_reason": "unexpected_react_state",
                }
            }

        react_result = run_react(
            conn,
            budget,
            observation={"claim": claim_context},
            decide=decide,
            max_steps=8,
            worker=True,
        )
        final = react_result["final"]
        status = final["status"]
        proposal = int(final.get("proposal_amount", 0))
        error_reason = final.get("error_reason")
        trace.add(
            "react_stop",
            {"reason": react_result["stopped"], "status": status},
        )

        if status != "quoted":
            if status not in {"needs_info", "escalated", "denied", "failed"}:
                status = "failed"
                error_reason = "invalid_react_result"
            return finish(
                conn,
                shape="single",
                claim_id=claim_id,
                seed=seed,
                status=status,
                proposal_amount=proposal,
                budget=budget,
                trace=trace,
                error_reason=error_reason,
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
                shape="single",
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
            shape="single",
            claim_id=claim_id,
            seed=seed,
            status="resolved",
            proposal_amount=proposal,
            budget=budget,
            trace=trace,
        )
    finally:
        conn.close()
