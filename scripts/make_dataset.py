"""Builds the demo dataset: the photos the robot "takes" and their true values.

    pip install poi-vision-inspector   (its synthetic photo generator)
    python scripts/make_dataset.py

Writes dataset/fotos/*.jpg, dataset/fotos.csv and dataset/respuestas-de-prueba.json.
The photos are committed, so the demo never needs this script to run; it is here to
regenerate or extend the set. Real photos can replace these: drop the file in
dataset/fotos and add its line to fotos.csv.

respuestas-de-prueba.json is what a model would be expected to answer for each photo,
written from the true values, keyed by image hash. It is the ``replay`` provider of the
inspector: CI and a first run without any model. It is not a model reading, and the
inspector says so in every result (model: replay).
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from poi_inspector.synth import Trouble, extinguisher_photo, gauge_photo

ROOT = Path(__file__).resolve().parent.parent / "dataset"
PHOTOS = ROOT / "fotos"

# PI-104, clean: every 0.25 bar from the normal 4.0 to the 7.0 of the worst peak.
GAUGE_CLEAN = [round(4.0 + 0.25 * i, 2) for i in range(13)]
# PI-104 with the problems a robot camera meets outdoors. Same values as a clean photo,
# so the robot can pick them instead now and then.
GAUGE_TROUBLE = [
    (5.5, "reflejo", Trouble(glare=0.75)),
    (6.75, "desenfoque", Trouble(blur=3.0, tilt_deg=8)),
]
EXTINGUISHER = [
    ("ok", "correcto", 1),
    ("ok", "correcto", 2),
    ("presion_baja", "presion_baja", 3),
    ("ausente", "ausente", 4),
]


def expected_answer(row: dict) -> dict:
    """What a good model should answer for this photo."""
    if row["poi"] == "PI-104":
        if row["condicion"] == "desenfoque":
            return {
                "value": None,
                "unit": None,
                "status_reason": "no_legible",
                "confidence": 0.2,
                "evidence": "the needle is blurred, the scale cannot be read",
            }
        confidence = 0.62 if row["condicion"] == "reflejo" else 0.94
        value = float(row["valor"])
        return {
            "value": value,
            "unit": "bar",
            "status_reason": "lectura_valida",
            "confidence": confidence,
            "evidence": f"needle at about {value} bar",
        }
    reason = row["condicion"]
    return {
        "value": row["valor"],
        "status_reason": reason,
        "confidence": 0.93,
        "evidence": f"extinguisher {reason.replace('_', ' ')}",
    }


def main() -> None:
    PHOTOS.mkdir(parents=True, exist_ok=True)
    rows = []

    for i, value in enumerate(GAUGE_CLEAN):
        name = f"pi-104_{value:.2f}.jpg"
        mild = Trouble(tilt_deg=(-4 + i % 5 * 2), noise=2)
        (PHOTOS / name).write_bytes(gauge_photo(value, trouble=mild, seed=100 + i))
        rows.append({"archivo": name, "poi": "PI-104", "valor": f"{value:.2f}", "condicion": "limpia"})

    for i, (value, condition, trouble) in enumerate(GAUGE_TROUBLE):
        name = f"pi-104_{value:.2f}_{condition}.jpg"
        (PHOTOS / name).write_bytes(gauge_photo(value, trouble=trouble, seed=200 + i))
        rows.append({"archivo": name, "poi": "PI-104", "valor": f"{value:.2f}", "condicion": condition})

    for state, label, seed in EXTINGUISHER:
        name = f"ext-12_{label}_{seed}.jpg"
        (PHOTOS / name).write_bytes(extinguisher_photo(state, trouble=Trouble(noise=2), seed=seed))
        value = "OK" if state == "ok" else "NOK"
        rows.append({"archivo": name, "poi": "EXT-12", "valor": value, "condicion": label})

    note = "Respuestas de prueba escritas desde los valores reales del dataset. No son lecturas de un modelo."
    answers = {"_nota": note}
    for row in rows:
        digest = hashlib.sha256((PHOTOS / row["archivo"]).read_bytes()).hexdigest()
        answers[digest] = expected_answer(row)
    text = json.dumps(answers, indent=1, ensure_ascii=False)
    (ROOT / "respuestas-de-prueba.json").write_text(text, encoding="utf-8")

    with (ROOT / "fotos.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["archivo", "poi", "valor", "condicion"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} photos in {PHOTOS}")


if __name__ == "__main__":
    main()
