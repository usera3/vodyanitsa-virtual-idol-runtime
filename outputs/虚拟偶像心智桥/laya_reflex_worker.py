"""Persistent GPU Laya System 1 worker for saved ARDY action decisions."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
import traceback


os.environ.setdefault("HF_HOME", r"D:\AI\Laya\cache")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
PROTOCOL = sys.stdout


def emit(**payload) -> None:
    PROTOCOL.write(json.dumps(payload, ensure_ascii=False) + "\n")
    PROTOCOL.flush()


def answer_value(answer: dict, key: str, default=None):
    value = answer.get(key, default)
    return value


def normalize_phrase(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value).casefold())


def exact_saved_action(user_text: str, actions: list[dict]) -> str | None:
    wanted = normalize_phrase(user_text)
    if not wanted:
        return None
    matches = []
    for item in actions:
        examples = [str(value) for value in item.get("examples", [])]
        if any(normalize_phrase(example) == wanted for example in examples):
            matches.append(str(item["id"]))
    return matches[0] if len(matches) == 1 else None


def select_reflex(
    similarities: dict[str, float], selected: str, choice_confidence: float,
    need_probability: float, *, semantic_threshold: float,
    semantic_override_threshold: float, semantic_margin: float,
    choice_threshold: float, motion_threshold: float,
) -> tuple[str | None, str, float]:
    """Fuse Laya choice and embeddings without trusting an ambiguous nearest match."""
    ranked = sorted(similarities.items(), key=lambda item: item[1], reverse=True)
    best_action, best_similarity = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
    margin = best_similarity - runner_up
    model_match = (
        best_similarity >= semantic_threshold
        and selected == best_action
        and choice_confidence >= choice_threshold
    )
    semantic_override = (
        best_similarity >= semantic_override_threshold
        and margin >= semantic_margin
    )
    if need_probability >= motion_threshold and (model_match or semantic_override):
        return best_action, "laya_choice" if model_match else "semantic_override", margin
    return None, "slow_planner", margin


def main() -> int:
    with contextlib.redirect_stdout(sys.stderr):
        from laya import Router
        from sentence_transformers import SentenceTransformer

        device = os.environ.get("LAYA_DEVICE", "cuda")
        router = Router(default="multilingual", max_loaded=1, device=device)
        router.preload(["multilingual"])
        embedder = SentenceTransformer(
            os.environ.get("LAYA_EMBED_MODEL", r"D:\AI\Laya\bge-small-zh-v1.5"),
            device=device,
        )
        # Ready means CUDA kernels are warm, not merely that weights are resident.
        embedder.encode(
            ["执行一个熟悉动作", "熟悉动作"],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        router.predict(
            "执行一个熟悉动作",
            {
                "needs_motion": {
                    "type": "noul",
                    "instructions": "Does the user want the virtual character to move now?",
                },
                "saved_action": {
                    "type": "choice",
                    "instructions": "Which saved motion closely matches?",
                    "criteria": {"__none__": "no match", "warmup": "familiar motion"},
                },
            },
            model="multilingual",
        )
    emit(status="ready", device=device, model="multilingual", embedder="bge-small-zh-v1.5")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if request.get("cmd") == "quit":
                return 0
            if request.get("cmd") != "decide":
                raise ValueError("unknown command")
            actions = [item for item in request.get("actions", []) if item.get("id")]
            if not actions:
                emit(status="decision", needs_motion=None, action_id=None, raw={})
                continue
            criteria = {"__none__": "No saved motion is a close semantic match; use slow planning."}
            action_texts = []
            for item in actions:
                description = "; ".join(
                    part for part in (
                        str(item.get("label") or ""),
                        str(item.get("prompt") or ""),
                        "examples: " + " | ".join(item.get("examples") or []),
                    ) if part
                )
                criteria[str(item["id"])] = description
                action_texts.append(
                    "；".join(
                        part for part in (
                            str(item.get("label") or ""),
                            "；".join(item.get("examples") or []),
                        ) if part
                    )
                )
            questions = {
                "needs_motion": {
                    "type": "noul",
                    "instructions": (
                        "Does the user want or clearly imply that the virtual character "
                        "should perform a visible body movement now?"
                    ),
                },
                "saved_action": {
                    "type": "choice",
                    "instructions": (
                        "Which saved ARDY motion is a close semantic match for the user's "
                        "requested movement? Choose __none__ unless the sequence and body "
                        "parts substantially match."
                    ),
                    "criteria": criteria,
                },
            }
            started = time.perf_counter()
            state_text = str(request.get("user_text") or "")
            with contextlib.redirect_stdout(sys.stderr):
                vectors = embedder.encode(
                    [state_text, *action_texts],
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                result = router.predict(
                    state_text,
                    questions,
                    model="multilingual",
                )
            answers = result.get("answers") or {}
            need = answers.get("needs_motion") or {}
            choice = answers.get("saved_action") or {}
            similarities = {
                str(item["id"]): round(float(vectors[0] @ vectors[index + 1]), 4)
                for index, item in enumerate(actions)
            }
            need_probability = float(need.get("noul") or 0.0)
            choice_confidence = float(choice.get("answer_confidence") or 0.0)
            selected = str(choice.get("choice") or "")
            reflex_action, decision_basis, semantic_margin = select_reflex(
                similarities, selected, choice_confidence, need_probability,
                semantic_threshold=float(os.environ.get("LAYA_SEMANTIC_THRESHOLD", "0.75")),
                semantic_override_threshold=float(os.environ.get("LAYA_SEMANTIC_OVERRIDE_THRESHOLD", "0.80")),
                semantic_margin=float(os.environ.get("LAYA_SEMANTIC_MARGIN", "0.12")),
                choice_threshold=float(os.environ.get("LAYA_CHOICE_THRESHOLD", "0.62")),
                motion_threshold=float(os.environ.get("LAYA_MOTION_THRESHOLD", "0.08")),
            )
            exact_action = exact_saved_action(state_text, actions)
            if (
                exact_action
                and need_probability >= float(os.environ.get("LAYA_MOTION_THRESHOLD", "0.08"))
            ):
                reflex_action = exact_action
                decision_basis = "exact_user_example"
            best_action = max(similarities, key=similarities.get)
            best_similarity = similarities[best_action]
            reflex_no_motion = (
                reflex_action is None
                and need_probability <= float(os.environ.get("LAYA_NO_MOTION_THRESHOLD", "0.02"))
            )
            emit(
                status="decision",
                needs_motion=need_probability,
                action_id=answer_value(choice, "choice"),
                need_answer=need,
                choice_answer=choice,
                semantic_scores=similarities,
                best_action_id=best_action,
                best_semantic_similarity=best_similarity,
                semantic_margin=round(semantic_margin, 4),
                decision_basis=decision_basis,
                exact_action_id=exact_action,
                reflex_action_id=reflex_action,
                reflex_no_motion=reflex_no_motion,
                elapsed_seconds=round(time.perf_counter() - started, 4),
                routing=result.get("routing"),
            )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            emit(status="error", message=str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
