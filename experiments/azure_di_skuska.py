"""Experiment: prevod PDF cenníka cez Azure AI Document Intelligence (model prebuilt-layout).

Výstup má rovnaký tvar ako docling_skuska.py, aby sa dali výsledky porovnať.

Prostriedok a prihlásenie sa berú z profilu v experiments/azure_di.toml:
    free      masha-documentintelligence (F0, 500 strán mesačne zadarmo, max. 2 strany na volanie)
    standard  kros-dev-documentintelligence (S0, platí sa za stranu)
Predvolený je profil podľa `aktivny` v súbore, iný sa vyberie prepínačom --profil.

Spustenie (z koreňa projektu, -I kvôli nedôveryhodným vstupom):
    .venv/Scripts/python.exe -I experiments/azure_di_skuska.py Cenniky/Prefa_Sucany/Prefa_kamenivo_2026.pdf
    .venv/Scripts/python.exe -I experiments/azure_di_skuska.py <pdf> --strany 3-6 --profil standard
    .venv/Scripts/python.exe -I experiments/azure_di_skuska.py <pdf> --strany 20 --z-json

Do Azure sa posielajú iba vybrané strany (vyrežú sa do malého PDF). Pri profile s limitom strán
na volanie (F0) sa posielajú po dávkach.
Výstup ide do experiments/vystup/<názov PDF>-azure/:
    dokument.md         celý obsah ako Markdown, ako ho vráti služba
    vysledok.json       kompletné odpovede služby po dávkach (strany, riadky, odseky, tabuľky)
    tabulka-NN.csv      každá tabuľka zvlášť, zlúčené bunky sú zopakované v každom políčku
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pandas as pd
import pypdfium2
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult, DocumentContentFormat, DocumentTable
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential

OUTPUT_ROOT = Path(__file__).parent / "vystup"
CONFIG_PATH = Path(__file__).parent / "azure_di.toml"


def load_profile(config_path: Path, name: str | None) -> tuple[str, dict]:
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    name = name or config["aktivny"]
    profiles = config["profily"]
    if name not in profiles:
        raise SystemExit(f"Profil '{name}' nie je v {config_path}, dostupné: {', '.join(profiles)}")
    return name, profiles[name]


def parse_pages(text: str | None, page_count: int) -> list[int]:
    """'3-5' -> [3, 4, 5], '20' -> [20], None -> všetky strany. Strany sú od 1."""
    if not text:
        return list(range(1, page_count + 1))
    first, _, last = text.partition("-")
    return list(range(int(first), min(int(last or first), page_count) + 1))


def cut_pages(pdf_path: Path, pages: list[int]) -> bytes:
    """Skopíruje vybrané strany do nového PDF bez zmeny obsahu (textová vrstva zostane)."""
    source = pypdfium2.PdfDocument(pdf_path)
    target = pypdfium2.PdfDocument.new()
    try:
        target.import_pages(source, [page - 1 for page in pages])
        buffer = io.BytesIO()
        target.save(buffer)
        return buffer.getvalue()
    finally:
        target.close()
        source.close()


def key_from_az(profile: dict) -> str:
    """Vypýta kľúč prostriedku cez Azure CLI. Kľúč ostane iba v pamäti tohto procesu."""
    az = shutil.which("az")
    if not az:
        raise SystemExit("Azure CLI (az) sa nenašlo, prihlás sa cez `az login` alebo použi prihlasenie = \"env\"")
    completed = subprocess.run(
        [az, "cognitiveservices", "account", "keys", "list",
         "--name", profile["prostriedok"], "--resource-group", profile["skupina"],
         "--subscription", profile["predplatne"], "--query", "key1", "--output", "tsv"],
        capture_output=True, text=True)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise SystemExit(f"Kľúč sa nepodarilo získať cez az: {completed.stderr.strip()[:300]}")
    return completed.stdout.strip()


def build_client(profile: dict) -> DocumentIntelligenceClient:
    mode = profile.get("prihlasenie", "entra")
    if mode == "entra":
        credential = DefaultAzureCredential()
    elif mode == "az-kluc":
        credential = AzureKeyCredential(key_from_az(profile))
    elif mode == "env":
        credential = AzureKeyCredential(os.environ["AZURE_DI_KEY"])
    else:
        raise SystemExit(f"Neznámy spôsob prihlásenia: {mode}")
    return DocumentIntelligenceClient(endpoint=profile["endpoint"], credential=credential)


def batches(pages: list[int], size: int) -> list[list[int]]:
    return [pages[start:start + size] for start in range(0, len(pages), size)]


def table_to_frame(table: DocumentTable) -> pd.DataFrame:
    """Mriežka tabuľky ako DataFrame. Riadky nad prvou dátovou bunkou tvoria názvy stĺpcov.

    Hlavička sa neurčuje podľa cell.kind samotného: Azure označí ako columnHeader aj popis v prvom
    stĺpci, ktorý sa ťahá cez celú skupinu riadkov (Rigips), a tie riadky by sa stratili.
    """
    grid = [[""] * table.column_count for _ in range(table.row_count)]
    content_rows = [cell.row_index for cell in table.cells if cell.kind in (None, "content")]
    header_rows = set(range(min(content_rows, default=0)))
    for cell in table.cells:
        for row in range(cell.row_index, cell.row_index + (cell.row_span or 1)):
            for column in range(cell.column_index, cell.column_index + (cell.column_span or 1)):
                grid[row][column] = (cell.content or "").replace("\n", " ").strip()

    header = sorted(header_rows)
    names = []
    for column in range(table.column_count):
        parts = []
        for row in header:
            text = grid[row][column]
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        names.append(".".join(parts) or f"stlpec_{column + 1}")
    body = [grid[row] for row in range(table.row_count) if row not in header_rows]
    return pd.DataFrame(body, columns=names)


def analyze(pdf: Path, pages: list[int], profile: dict) -> list[tuple[list[int], AnalyzeResult]]:
    client = build_client(profile)
    limit_bytes = profile["max_velkost_mb"] * 1024 * 1024
    parts = []
    for index, chunk in enumerate(batches(pages, profile["max_stran_na_volanie"])):
        payload = cut_pages(pdf, chunk)
        if len(payload) > limit_bytes:
            raise SystemExit(f"Strany {chunk} majú {len(payload) / 1048576:.1f} MB, profil dovoľuje "
                             f"{profile['max_velkost_mb']} MB. Skús menej strán alebo profil standard.")
        if index:
            time.sleep(1)  # F0 dovoľuje 1 požiadavku za sekundu
        poller = client.begin_analyze_document(
            "prebuilt-layout", body=io.BytesIO(payload), output_content_format=DocumentContentFormat.MARKDOWN)
        parts.append((chunk, poller.result()))
        print(f"  dávka strán {chunk[0]}–{chunk[-1]} hotová", flush=True)
    return parts


def load_saved(path: Path, pages: list[int]) -> list[tuple[list[int], AnalyzeResult]]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    if "casti" not in saved:  # starší formát: jedna odpoveď pre všetky strany
        return [(pages, AnalyzeResult(saved))]
    return [(part["strany"], AnalyzeResult(part["vysledok"])) for part in saved["casti"]]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Prevod PDF cenníka cez Azure AI Document Intelligence")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--strany", help="rozsah strán, napr. 3-5 alebo 20")
    parser.add_argument("--profil", help="profil z azure_di.toml (free, standard); predvolený je `aktivny`")
    parser.add_argument("--konfig", type=Path, default=CONFIG_PATH, help="cesta ku konfiguračnému súboru")
    parser.add_argument("--z-json", action="store_true",
                        help="nevolať Azure, len znovu vytvoriť CSV z uloženého vysledok.json (zadarmo)")
    args = parser.parse_args()

    profile_name, profile = load_profile(args.konfig, args.profil)
    source = pypdfium2.PdfDocument(args.pdf)
    page_count = len(source)
    source.close()
    pages = parse_pages(args.strany, page_count)
    out_dir = OUTPUT_ROOT / f"{args.pdf.stem}-azure"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Súbor: {args.pdf}")
    if args.z_json:
        print("Režim: z uloženého vysledok.json, Azure sa nevolá")
    else:
        cost = len(pages) * profile["cena_za_1000_stran_eur"] / 1000
        print(f"Profil: {profile_name} ({profile['popis']})")
        print(f"Strany: {pages[0]}–{pages[-1]} ({len(pages)}), odhad ceny: {cost:.3f} €")

    started = time.perf_counter()
    if args.z_json:
        parts = load_saved(out_dir / "vysledok.json", pages)
    else:
        parts = analyze(args.pdf, pages, profile)
        (out_dir / "dokument.md").write_text(
            "\n\n".join(result.content or "" for _, result in parts), encoding="utf-8")
        saved = {"profil": profile_name, "casti": [{"strany": chunk, "vysledok": result.as_dict()}
                                                  for chunk, result in parts]}
        (out_dir / "vysledok.json").write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    elapsed = time.perf_counter() - started
    for old in out_dir.glob("tabulka-*.csv"):
        old.unlink()

    tables = [(chunk, table) for chunk, result in parts for table in (result.tables or [])]
    paragraphs = sum(len(result.paragraphs or []) for _, result in parts)
    print(f"Čas: {elapsed:.1f} s, odseky: {paragraphs}, tabuľky: {len(tables)}")

    for index, (chunk, table) in enumerate(tables, start=1):
        frame = table_to_frame(table)
        frame.to_csv(out_dir / f"tabulka-{index:02d}.csv", index=False, encoding="utf-8-sig")
        # Služba čísluje strany vyrezaného PDF od 1, preložíme ich späť na strany pôvodného cenníka.
        region = (table.bounding_regions or [None])[0]
        page = chunk[region.page_number - 1] if region and region.page_number <= len(chunk) else "?"
        print(f"\n=== Tabuľka {index} (strana {page}), {frame.shape[0]} riadkov × {frame.shape[1]} stĺpcov")
        print("Stĺpce:", " | ".join(str(column) for column in frame.columns))
        print(frame.head(6).to_string(max_colwidth=28))

    print(f"\nVýstup: {out_dir}")


if __name__ == "__main__":
    main()
