"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    from lab9.graders import GRADERS

    problems: list[str] = []

    # 1) NaN / inf in aggregate metrics
    for mid, val in (pr.get("metrics") or {}).items():
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            problems.append(f"metric {mid} is NaN/inf")

    # 2) row count matches items x seeds
    manifest = pr.get("manifest") or {}
    expected_rows = int(manifest.get("items", 0)) * len(manifest.get("seeds") or [])
    if len(pr.get("rows") or []) != expected_rows:
        problems.append(f"row count {len(pr.get('rows') or [])} != items×seeds {expected_rows}")

    # 3) (item_id, seed) set identical to baseline
    pr_keys = {(r["item_id"], r["seed"]) for r in pr.get("rows") or []}
    base_keys = {(r["item_id"], r["seed"]) for r in base.get("rows") or []}
    if pr_keys != base_keys:
        missing, extra = base_keys - pr_keys, pr_keys - base_keys
        problems.append(f"(item,seed) set differs from baseline: -{len(missing)} +{len(extra)}")

    # 4) evaluator failures / None for a graded spec metric
    if pr.get("evaluator_failures"):
        problems.append(f"{len(pr['evaluator_failures'])} evaluator failure(s)")
    graded = [m["id"] for m in spec.get("metrics", []) if m.get("id") in GRADERS]
    for r in pr.get("rows") or []:
        for mid in graded:
            if r.get(mid) is None:
                problems.append(f"row {r['item_id']}@{r['seed']} has None for graded metric {mid}")
                break

    # 5) ratchet: spec may not be looser than base_spec for the same eval-set version
    if base_spec is not None and spec.get("eval_set_version") == base_spec.get("eval_set_version"):
        base_by_id = {m["id"]: m for m in base_spec.get("metrics", [])}
        for m in spec.get("metrics", []):
            mid = m["id"]
            b = base_by_id.get(mid)
            if b is None:
                continue  # a new metric is allowed
            bth, th = b.get("threshold", {}), m.get("threshold", {})
            if "max" in bth and ("max" not in th or th["max"] > bth["max"]):
                problems.append(f"ratchet: {mid} max loosened")
            if "min" in bth and ("min" not in th or th["min"] < bth["min"]):
                problems.append(f"ratchet: {mid} min loosened")
            if b.get("blocking") and not m.get("blocking"):
                problems.append(f"ratchet: {mid} no longer blocking")
            if bth.get("no_significant_drop") and not th.get("no_significant_drop"):
                problems.append(f"ratchet: {mid} dropped no_significant_drop")
        # slices ratchet
        base_slices = {(s["name"], s["metric"]): s for s in base_spec.get("slices") or []}
        cur_slices = {(s["name"], s["metric"]): s for s in spec.get("slices") or []}
        for key, b in base_slices.items():
            sid = f"slice:{key[0]}/{key[1]}"
            s = cur_slices.get(key)
            if s is None:
                problems.append(f"ratchet: {sid} disappeared")
                continue
            if b.get("blocking") and not s.get("blocking"):
                problems.append(f"ratchet: {sid} no longer blocking")
            if "min" in b and ("min" not in s or s["min"] < b["min"]):
                problems.append(f"ratchet: {sid} min loosened")
            if b.get("no_significant_drop") and not s.get("no_significant_drop"):
                problems.append(f"ratchet: {sid} dropped no_significant_drop")

    # 6) slice tags per item must match the baseline
    def tags(report: dict) -> dict[str, frozenset]:
        out: dict[str, frozenset] = {}
        for r in report.get("rows") or []:
            out.setdefault(r["item_id"], frozenset(r.get("slices") or []))
        return out

    pr_tags, base_tags = tags(pr), tags(base)
    for item, b in base_tags.items():
        if item in pr_tags and pr_tags[item] != b:
            problems.append(f"slice tags for {item} changed: {sorted(b)} -> {sorted(pr_tags[item])}")

    return problems


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    bs = spec.get("bootstrap", {})
    blocked, lines = [], []
    for sl in spec.get("slices") or []:
        name, metric = sl["name"], sl["metric"]
        v = per_item(pr, metric, slice_name=name)
        val = round(sum(v.values()) / len(v), 4) if v else None
        sid = f"slice:{name}/{metric}"
        verdict, note = "ok", ""
        if val is None:
            verdict = "empty"
        elif "min" in sl and val < sl["min"]:
            verdict = "fail"
        if sl.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, metric, slice_name=name), per_item(base, metric, slice_name=name)
            keys = sorted(set(a) & set(b))
            if keys:
                d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                                   n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
                note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                if hi < 0:
                    verdict = "fail"
        if verdict == "fail" and sl.get("blocking"):
            blocked.append(sid)
        label = {"ok": "ok", "empty": "EMPTY"}.get(verdict, "BLOCK" if sl.get("blocking") else "warn")
        th = {k: sl[k] for k in ("min", "no_significant_drop") if k in sl}
        lines.append(f"  {sid:<28} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
