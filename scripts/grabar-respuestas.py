"""Asks Gemini once about every real photo of the dataset and prints its answers, box
included, as JSON for dataset/respuestas-de-prueba.json. With them, the recorded
readings the robot's round replays are what a model really saw in those photos.

Run it where the stack runs, with GEMINI_API_KEY in .env (the public profile):

    python3 scripts/grabar-respuestas.py           # every photo
    python3 scripts/grabar-respuestas.py pi-104    # only the photos whose name starts so

It changes nothing: it only prints. Generated photos are skipped.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("value", "unit", "status_reason", "confidence", "evidence", "bbox_2d", "other_boxes")


def ask(photo: Path, poi: str) -> dict:
    command = [
        "docker", "compose", "run", "--rm", "--no-deps", "-T", "--entrypoint", "poi-inspect",
        "-v", f"{ROOT / 'config' / 'inspector.gemini.yaml'}:/grabar/gemini.yaml:ro",
        "-v", f"{photo}:/grabar/foto.jpg:ro",
        "inspector", "--config", "/grabar/gemini.yaml", "read", "/grabar/foto.jpg", "--poi", poi,
    ]  # fmt: skip
    done = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    if done.returncode:
        raise RuntimeError(done.stderr.strip()[-400:])
    result = json.loads(done.stdout)
    raw = result.get("trace", {}).get("raw")
    try:  # the model's own answer, as the replay will give it back
        answer = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        answer = None
    if not isinstance(answer, dict):
        answer = {key: result.get(key) for key in FIELDS}
    return {key: answer.get(key) for key in FIELDS}


def main() -> int:
    only = tuple(prefix.lower() for prefix in sys.argv[1:])
    answers: dict[str, dict] = {}
    with open(ROOT / "dataset" / "fotos.csv", encoding="utf-8") as rows:
        for row in csv.DictReader(rows):
            if not row["licencia"].startswith("CC"):
                continue
            if only and not row["archivo"].lower().startswith(only):
                continue
            photo = ROOT / "dataset" / "fotos" / row["archivo"]
            try:
                answer = ask(photo, row["poi"])
            except RuntimeError as error:
                print(f"{row['archivo']}: ERROR {error}", file=sys.stderr)
                continue
            answers[hashlib.sha256(photo.read_bytes()).hexdigest()] = {"archivo": row["archivo"], **answer}
            reading = f"{answer['value']} {answer['unit'] or ''}"
            summary = f"{reading} · {answer['confidence']} · caja {answer['bbox_2d']} · otras {answer['other_boxes']}"
            print(f"{row['archivo']}: {summary}", file=sys.stderr)
    print(json.dumps(answers, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
