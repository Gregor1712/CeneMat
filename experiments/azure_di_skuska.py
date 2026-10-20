"""Experiment: prevod PDF cenníka cez Azure AI Document Intelligence (model prebuilt-layout).

Výstup má rovnaký tvar ako docling_skuska.py, aby sa dali výsledky porovnať.

Prihlásenie:
    - ak je nastavená premenná AZURE_DI_KEY, použije sa kľúč,
    - inak Entra ID cez DefaultAzureCredential (stačí `az login` a rola Cognitive Services User na prostriedku).
Endpoint: --endpoint alebo premenná AZURE_DI_ENDPOINT.

Spustenie (z koreňa projektu, -I kvôli nedôveryhodným vstupom):
    .venv/Scripts/python.exe -I experiments/azure_di_skuska.py Cenniky/Porfix/Porfix_cennik_2026-09-01.pdf --endpoint https://<prostriedok>.cognitiveservices.azure.com/
    .venv/Scripts/python.exe -I experiments/azure_di_skuska.py <pdf> --strany 20

Do Azure sa posielajú iba vybrané strany (vyrežú sa do malého PDF), platí sa za stranu.
Výstup ide do experiments/vystup/<názov PDF>-azure/:
    dokument.md         celý obsah ako Markdown, ako ho vráti služba
    vysledok.json       kompletná odpoveď služby (strany, riadky, odseky, tabuľky so súradnicami)
    tabulka-NN.csv      každá tabuľka zvlášť, zlúčené bunky sú zopakované v každom políčku
"""
import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pypdfium2
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult, DocumentContentFormat, DocumentTable
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential

OUTPUT_ROOT = Path(__file__).parent / "vystup"


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


def build_client(endpoint: str) -> DocumentIntelligenceClient:
    key = os.environ.get("AZURE_DI_KEY")
    credential = AzureKeyCredential(key) if key else DefaultAzureCredential()
    return DocumentIntelligenceClient(endpoint=endpoint, credential=credential)


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


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Prevod PDF cenníka cez Azure AI Document Intelligence")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--strany", help="rozsah strán, napr. 3-5 alebo 20")
    parser.add_argument("--endpoint", default=os.environ.get("AZURE_DI_ENDPOINT"),
                        help="https://<prostriedok>.cognitiveservices.azure.com/")
    parser.add_argument("--z-json", action="store_true",
                        help="nevolať Azure, len znovu vytvoriť CSV z uloženého vysledok.json (zadarmo)")
    args = parser.parse_args()
    if not args.endpoint and not args.z_json:
        parser.error("chýba --endpoint alebo premenná AZURE_DI_ENDPOINT")

    source = pypdfium2.PdfDocument(args.pdf)
    page_count = len(source)
    source.close()
    pages = parse_pages(args.strany, page_count)
    out_dir = OUTPUT_ROOT / f"{args.pdf.stem}-azure"
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    if args.z_json:
        result = AnalyzeResult(json.loads((out_dir / "vysledok.json").read_text(encoding="utf-8")))
    else:
        poller = build_client(args.endpoint).begin_analyze_document(
            "prebuilt-layout",
            body=io.BytesIO(cut_pages(args.pdf, pages)),
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        result = poller.result()
        (out_dir / "dokument.md").write_text(result.content or "", encoding="utf-8")
        (out_dir / "vysledok.json").write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    elapsed = time.perf_counter() - started
    for old in out_dir.glob("tabulka-*.csv"):
        old.unlink()

    # Služba čísluje strany vyrezaného PDF od 1, preložíme ich späť na strany pôvodného cenníka.
    original_page = {index: page for index, page in enumerate(pages, start=1)}
    print(f"Súbor: {args.pdf}")
    print(f"Strany: {pages[0]}–{pages[-1]} ({len(pages)}), model: {result.model_id}, čas: {elapsed:.1f} s")
    print(f"Odseky: {len(result.paragraphs or [])}, tabuľky: {len(result.tables or [])}")

    for index, table in enumerate(result.tables or [], start=1):
        frame = table_to_frame(table)
        frame.to_csv(out_dir / f"tabulka-{index:02d}.csv", index=False, encoding="utf-8-sig")
        region = (table.bounding_regions or [None])[0]
        page = original_page.get(region.page_number, "?") if region else "?"
        print(f"\n=== Tabuľka {index} (strana {page}), {frame.shape[0]} riadkov × {frame.shape[1]} stĺpcov")
        print("Stĺpce:", " | ".join(str(column) for column in frame.columns))
        print(frame.head(6).to_string(max_colwidth=28))

    print(f"\nVýstup: {out_dir}")


if __name__ == "__main__":
    main()
