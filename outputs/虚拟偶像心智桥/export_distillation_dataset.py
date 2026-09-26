"""Export bridge audit events into a local, training-ready decision dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


OUTCOME_STATES = {
    "reflex_dispatched",
    "reflex_no_motion",
    "planned_dispatched",
    "no_motion",
    "motion_plan_failed",
}


def event_id(row: dict[str, Any]) -> str:
    return str(
        row.get("source_event_id")
        or (row.get("intent") or {}).get("source_event_id")
        or ""
    )


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def target_for(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    outcome = next((row for row in reversed(rows) if row.get("state") in OUTCOME_STATES), None)
    if outcome is None:
        return None
    state = outcome.get("state")
    if state in {"reflex_no_motion", "no_motion"}:
        return {"mode": "none", "source": outcome.get("route") or "teacher"}
    if state == "reflex_dispatched":
        return {
            "mode": "catalog",
            "source": "laya",
            "action_id": (outcome.get("saved_action") or {}).get("id"),
            "result": outcome.get("result"),
        }
    if state == "planned_dispatched":
        plan = outcome.get("plan") or {}
        saved = outcome.get("saved_action") or {}
        return {
            "mode": plan.get("mode") or "catalog",
            "source": outcome.get("route") or "agnes",
            "action_id": saved.get("id") or plan.get("action_id"),
            "ardy_prompt": plan.get("ardy_prompt") or "",
            "duration_seconds": plan.get("duration_seconds"),
            "mask": plan.get("mask"),
            "result": outcome.get("result"),
        }
    return {
        "mode": "error",
        "source": "pipeline",
        "error": outcome.get("error"),
    }


def export(audit_path: Path, library_path: Path, output_path: Path) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in load_rows(audit_path):
        key = event_id(row)
        if key:
            grouped.setdefault(key, []).append(row)
    library = json.loads(library_path.read_text(encoding="utf-8"))
    actions = {
        item["id"]: {
            "label": item.get("label"),
            "ardy_prompt": item.get("ardy_prompt"),
            "user_examples": item.get("user_examples", []),
            "model": item.get("model"),
        }
        for item in library.get("actions", [])
        if isinstance(item, dict) and item.get("id")
    }
    records = []
    for key, rows in grouped.items():
        planning = next((row for row in rows if row.get("state") == "planning_motion"), None)
        target = target_for(rows)
        if not planning or target is None:
            continue
        laya = next((row.get("decision") for row in rows if row.get("state") == "laya_decision"), None)
        teacher = next((row.get("plan") for row in rows if row.get("state") == "motion_planned"), None)
        candidate_ids = list((laya or {}).get("semantic_scores", {}).keys())
        records.append({
            "schema": "virtual-idol-motion-decision-v1",
            "event_id": key,
            "timestamp": planning.get("updated_at"),
            "input": {
                "user_text": planning.get("user_text"),
                "candidate_actions": {
                    action_id: actions.get(action_id, {"id": action_id})
                    for action_id in candidate_ids
                },
            },
            "reflex": laya,
            "teacher": teacher,
            "target": target,
            "provenance": {
                "reflex_model": "laya-0.3.20-multilingual+bge-small-zh-v1.5",
                "teacher_model": "agnes-3.0-flash",
                "motion_model": "ARDY-Core-RP-20FPS-Horizon40+MiniLM",
            },
        })
    records.sort(key=lambda item: (item.get("timestamp") or "", item["event_id"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )
    modes: dict[str, int] = {}
    for item in records:
        mode = str(item["target"].get("mode"))
        modes[mode] = modes.get(mode, 0) + 1
    return {"records": len(records), "modes": modes, "output": str(output_path)}


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, default=root / "审计记录.jsonl")
    parser.add_argument(
        "--library", type=Path,
        default=root.parent / "Mocap动作总线" / "clips" / "generated" / "library.json",
    )
    parser.add_argument(
        "--output", type=Path,
        default=root / "训练数据" / "动作决策蒸馏.jsonl",
    )
    args = parser.parse_args()
    print(json.dumps(export(args.audit, args.library, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
