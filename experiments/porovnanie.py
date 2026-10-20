"""Porovnanie výstupov dvoch nástrojov (napr. Docling a Azure Document Intelligence) nad tým istým PDF.

Spustenie (z koreňa projektu):
    .venv/Scripts/python.exe -I experiments/porovnanie.py experiments/vystup/Porfix_cennik_2026-09-01 experiments/vystup/Porfix_cennik_2026-09-01-azure
    .venv/Scripts/python.exe -I experiments/porovnanie.py <priečinok A> <priečinok B> --hladaj 76.20,93.73

Pre každý priečinok s tabulka-NN.csv vypíše:
    - počet tabuliek a dátových riadkov,
    - kontrolu DPH po riadkoch: posledné ceny v riadku sú bez DPH a s DPH (rozloženie Porfix),
    - kontrolu DPH po stĺpcoch: cena bez DPH a pod ňou cena s DPH (rozloženie Bramac),
    - ktoré čísla našiel iba jeden z nástrojov.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

VAT = 1.23
NUMBER = re.compile(r"^-?\d{1,3}(?:[  ]\d{3})*(?:[.,]\d+)?$|^-?\d+(?:[.,]\d+)?$")


def to_number(text: str) -> float | None:
    text = text.strip().rstrip("*").replace("€", "").strip()
    if not NUMBER.match(text):
        return None
    return float(text.replace(" ", "").replace(" ", "").replace(",", "."))


def load_rows(folder: Path) -> list[tuple[str, list[str]]]:
    rows = []
    for path in sorted(folder.glob("tabulka-*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            rows.extend((path.stem, row) for row in reader)
    return rows


def vat_by_row(rows: list[tuple[str, list[str]]]) -> tuple[int, int]:
    """Riadok sedí, ak posledné 2, 4 alebo 6 čísel tvoria bloky bez DPH a s DPH."""
    checked = passed = 0
    for _, row in rows:
        numbers = [n for cell in row for token in cell.split() if (n := to_number(token)) is not None]
        if len(numbers) < 2:
            continue
        checked += 1
        for size in (6, 4, 2):
            if len(numbers) >= size:
                net, gross = numbers[-size:-size // 2], numbers[-size // 2:]
                if all(abs(g - n * VAT) <= 0.011 for n, g in zip(net, gross)):
                    passed += 1
                    break
    return checked, passed


def vat_by_column(rows: list[tuple[str, list[str]]]) -> int:
    """Počet dvojíc „cena bez DPH a pod ňou cena s DPH" v rovnakom stĺpci tej istej tabuľky."""
    pairs = 0
    tables: dict[str, list[list[str]]] = {}
    for table, row in rows:
        tables.setdefault(table, []).append(row)
    for table_rows in tables.values():
        width = max((len(r) for r in table_rows), default=0)
        for column in range(width):
            cells = [r[column] for r in table_rows if column < len(r) and r[column].strip()]
            for upper, lower in zip(cells, cells[1:]):
                for a in upper.split():
                    for b in lower.split():
                        x, y = to_number(a), to_number(b)
                        if x and y and "." in a + b and abs(y - x * VAT) <= 0.011 and x < y:
                            pairs += 1
    return pairs


def numbers_in(rows: list[tuple[str, list[str]]]) -> set[float]:
    return {n for _, row in rows for cell in row for token in cell.split() if (n := to_number(token)) is not None}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Porovnanie výstupov dvoch nástrojov")
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    parser.add_argument("--hladaj", help="čiarkou oddelené hodnoty, ktoré sa majú v tabuľkách hľadať")
    args = parser.parse_args()

    loaded = {folder: load_rows(folder) for folder in (args.a, args.b)}
    for folder, rows in loaded.items():
        tables = len({table for table, _ in rows})
        checked, passed = vat_by_row(rows)
        print(f"\n## {folder.name}")
        print(f"tabuľky: {tables}, dátové riadky: {len(rows)}")
        print(f"DPH po riadkoch: {passed} z {checked} riadkov s číslami sedí")
        print(f"DPH po stĺpcoch: {vat_by_column(rows)} dvojíc bez/s DPH")
        if args.hladaj:
            for value in args.hladaj.split(","):
                hits = sum(cell.count(value) for _, row in rows for cell in row)
                print(f"hodnota {value}: {hits}× v bunkách")

    numbers_a, numbers_b = (numbers_in(rows) for rows in loaded.values())
    only_a, only_b = sorted(numbers_a - numbers_b), sorted(numbers_b - numbers_a)
    print(f"\n## Čísla v bunkách (rôzne hodnoty)")
    print(f"spoločné: {len(numbers_a & numbers_b)}, iba {args.a.name}: {len(only_a)}, iba {args.b.name}: {len(only_b)}")
    print(f"iba {args.a.name}:", ", ".join(f"{n:g}" for n in only_a[:20]))
    print(f"iba {args.b.name}:", ", ".join(f"{n:g}" for n in only_b[:20]))


if __name__ == "__main__":
    main()
