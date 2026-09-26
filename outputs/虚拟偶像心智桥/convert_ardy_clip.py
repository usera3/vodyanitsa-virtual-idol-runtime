"""Run ARDY NPZ -> action-bus clip conversion inside the ARDY environment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bus-source", type=Path, required=True)
    parser.add_argument("--npz", type=Path)
    parser.add_argument("--clip", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.bus_source.resolve()))
    from ardy_npz_to_clip import write_clip

    if args.worker:
        print(json.dumps({"status": "ready"}), flush=True)
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if request.get("cmd") == "quit":
                    return
                npz = Path(request["npz"]).resolve()
                clip = Path(request["clip"]).resolve()
                clip.parent.mkdir(parents=True, exist_ok=True)
                result = write_clip(npz, clip)
                print(json.dumps({"status": "done", **result}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        return
    if args.npz is None or args.clip is None:
        parser.error("--npz and --clip are required unless --worker is used")
    args.clip.parent.mkdir(parents=True, exist_ok=True)
    result = write_clip(args.npz.resolve(), args.clip.resolve())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
