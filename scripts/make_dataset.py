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

from poi_inspector.synth import Trouble, gauge_photo

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

# Real photos from Wikimedia Commons (CC BY, CC BY-SA, CC0), one per point of interest
# of the round, read by hand. Only resized: the originals are not in the repository.
#   python scripts/make_dataset.py --originales <folder with the Wikimedia files>
# Without --originales the processed photos already in dataset/fotos are kept.
COMMONS = "https://commons.wikimedia.org/wiki/File:"
REAL = [
    # file in dataset/fotos, poi, true value, condition, original, author, license, expected answer
    ("pi-610_21.0.jpg", "PI-610", "21.0", "real", "Bar gauge.jpg", "Eng.dilan86", "CC BY-SA 4.0",
     {"value": 21.0, "unit": "bar", "status_reason": "lectura_valida", "confidence": 0.9,
      "evidence": "WIKA 0-25 bar gauge, needle just past 20"}),
    ("pi-201_1.7.jpg", "PI-201", "1.7", "real", "A pressure gauge attached to a heating system.jpg",
     "Shixart1985", "CC BY 2.0",
     {"value": 1.7, "unit": "bar", "status_reason": "lectura_valida", "confidence": 0.88,
      "evidence": "red pressure needle between 1.5 and 2 bar on the upper scale"}),
    ("piv-801_-20.jpg", "PIV-801", "-20", "real", "Pressure-vacuum gauge transformer.jpg",
     "VeRdUgO PY", "CC BY-SA 4.0",
     {"value": -20.0, "unit": "kPa", "status_reason": "lectura_valida", "confidence": 0.85,
      "evidence": "vacuum/pressure gauge, needle on -20 kPa"}),
    ("pi-402_desgastado.jpg", "PI-402", "", "desgastado",
     "Old pressure gauge showing measurements in a worn condition located in a workshop.jpg",
     "Shixart1985", "CC BY 2.0",
     {"value": None, "unit": None, "status_reason": "no_legible", "confidence": 0.3,
      "evidence": "dirty, worn dial: the scale numbers cannot be read"}),
    ("ext-12_correcto.jpg", "EXT-12", "OK", "correcto", "A Fire Extinguisher.jpg",
     "Paul The Writer", "CC0 1.0",
     {"value": "OK", "status_reason": "correcto", "confidence": 0.92,
      "evidence": "CO2 extinguisher hanging on its wall bracket"}),
    ("ext-12_en_el_suelo.jpg", "EXT-12", "NOK", "fuera_de_soporte", "CO2 extinguisher.jpg",
     "Eunosfire986", "CC0 1.0",
     {"value": "NOK", "status_reason": "fuera_de_soporte", "confidence": 0.9,
      "evidence": "CO2 extinguisher lying on the floor, not on its bracket"}),
    ("ext-14_correcto.jpg", "EXT-14", "OK", "correcto", "Fire extinguisher 1 2.jpg",
     "Nizil Shah", "CC BY-SA 4.0",
     {"value": "OK", "status_reason": "correcto", "confidence": 0.86,
      "evidence": "extinguisher on the wall under its sign"}),
    ("ext-14_fuera_de_soporte.jpg", "EXT-14", "NOK", "fuera_de_soporte", "Dry powder extinguisher.jpg",
     "Eunosfire986", "CC0 1.0",
     {"value": "NOK", "status_reason": "fuera_de_soporte", "confidence": 0.9,
      "evidence": "powder extinguishers standing on the floor, off their bracket"}),
]
MAX_SIDE = 1280


def license_url(name: str) -> str:
    if name.startswith("CC0"):
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    kind, version = name.removeprefix("CC ").split()
    return f"https://creativecommons.org/licenses/{kind.lower()}/{version}/"


def resize(original: Path, target: Path) -> None:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = None  # some originals are 120 MP
    with Image.open(original) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        im.save(target, "JPEG", quality=85, optimize=True)  # no metadata kept


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
    return next(r[7] for r in REAL if r[0] == row["archivo"])


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--originales", type=Path, help="folder with the original Wikimedia photos")
    args = parser.parse_args(argv)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    rows = []
    synthetic = {"autor": "generada con poi_inspector.synth", "licencia": "MIT", "fuente": ""}

    for i, value in enumerate(GAUGE_CLEAN):
        name = f"pi-104_{value:.2f}.jpg"
        mild = Trouble(tilt_deg=(-4 + i % 5 * 2), noise=2)
        (PHOTOS / name).write_bytes(gauge_photo(value, trouble=mild, seed=100 + i))
        rows.append({"archivo": name, "poi": "PI-104", "valor": f"{value:.2f}", "condicion": "limpia", **synthetic})

    for i, (value, condition, trouble) in enumerate(GAUGE_TROUBLE):
        name = f"pi-104_{value:.2f}_{condition}.jpg"
        (PHOTOS / name).write_bytes(gauge_photo(value, trouble=trouble, seed=200 + i))
        rows.append({"archivo": name, "poi": "PI-104", "valor": f"{value:.2f}", "condicion": condition, **synthetic})

    credits = []
    for name, poi, value, condition, original, author, license_name, _ in REAL:
        if args.originales:
            resize(args.originales / original, PHOTOS / name)
        if not (PHOTOS / name).exists():
            raise SystemExit(f"{name} is missing: run with --originales <folder>")
        source = COMMONS + original.replace(" ", "_")
        rows.append({"archivo": name, "poi": poi, "valor": value, "condicion": condition,
                     "autor": author, "licencia": license_name, "fuente": source})
        credits.append(f"- `{name}` ({poi}): *{original}*, {author}, "
                       f"[{license_name}]({license_url(license_name)}), [Wikimedia Commons]({source}). Redimensionada.")

    note = "Respuestas de prueba escritas desde los valores reales del dataset. No son lecturas de un modelo."
    answers = {"_nota": note}
    for row in rows:
        digest = hashlib.sha256((PHOTOS / row["archivo"]).read_bytes()).hexdigest()
        answers[digest] = expected_answer(row)
    text = json.dumps(answers, indent=1, ensure_ascii=False)
    (ROOT / "respuestas-de-prueba.json").write_text(text, encoding="utf-8")

    with (ROOT / "fotos.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["archivo", "poi", "valor", "condicion", "autor", "licencia", "fuente"]
        )
        writer.writeheader()
        writer.writerows(rows)
    header = (
        "# Créditos de las fotos\n\n"
        "Las fotos `pi-104_*` están generadas con `poi_inspector.synth` (MIT). Las demás son fotos reales "
        "de Wikimedia Commons, solo redimensionadas, y conservan su licencia:\n\n"
    )
    (PHOTOS / "CREDITOS.md").write_text(header + "\n".join(credits) + "\n", encoding="utf-8")
    print(f"{len(rows)} photos in {PHOTOS}")


if __name__ == "__main__":
    main()
