"""Experiment: prevod jedného PDF cenníka cez Docling.

Spustenie (z koreňa projektu, -I kvôli nedôveryhodným vstupom):
    .venv/Scripts/python.exe -I experiments/docling_skuska.py Cenniky/Porfix/Porfix_cennik_2026-09-01.pdf
    .venv/Scripts/python.exe -I experiments/docling_skuska.py <pdf> --strany 3-4 --rezim fast

Výstup ide do experiments/vystup/<názov PDF>/:
    dokument.md         celý dokument ako Markdown (text, nadpisy, tabuľky)
    dokument.json       DoclingDocument so všetkými prvkami a ich polohou na strane
    tabulka-NN.csv      každá rozpoznaná tabuľka zvlášť
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
import pypdfium2
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import OcrMode, PdfPipelineOptions, RapidOcrOptions, TableFormerMode
from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

OUTPUT_ROOT = Path(__file__).parent / "vystup"


def parse_pages(text: str | None) -> tuple[int, int]:
    """'3-5' -> (3, 5), '4' -> (4, 4), None -> celý dokument. Strany sú od 1."""
    if not text:
        return 1, sys.maxsize
    first, _, last = text.partition("-")
    return int(first), int(last or first)


def build_converter(mode: str, ocr: str) -> DocumentConverter:
    options = PdfPipelineOptions()
    # vyp:  text len z textovej vrstvy PDF (rýchle, ale vidí aj neviditeľný text, napr. Bramac)
    # auto: OCR iba tam, kde textová vrstva chýba
    # cela: celá strana cez OCR z obrázka, berie sa len to, čo je naozaj vidieť
    options.do_ocr = ocr != "vyp"
    if ocr == "cela":
        # backend torch: PyTorch už máme kvôli Doclingu, onnxruntime netreba
        options.ocr_options = RapidOcrOptions(mode=OcrMode.FULL_PAGE, lang=["sk"], backend="torch")
    # TableFormer: model, ktorý z obrázka tabuľky určí riadky, stĺpce a zlúčené bunky.
    options.do_table_structure = True
    options.table_structure_options.mode = TableFormerMode.ACCURATE if mode == "accurate" else TableFormerMode.FAST
    # Text buniek sa berie z textovej vrstvy PDF, model určuje iba mriežku.
    options.table_structure_options.do_cell_matching = True

    options.accelerator_options = AcceleratorOptions(num_threads=os.cpu_count() or 4, device=AcceleratorDevice.CPU)

    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
    })


def render_page(pdf_path: Path, page_no: int, target: Path, scale: float = 3.0) -> Path:
    """Vykreslí stranu do PNG, obrázok nemá textovú vrstvu, len to, čo je vidieť."""
    document = pypdfium2.PdfDocument(pdf_path)
    try:
        document[page_no - 1].render(scale=scale).to_pil().save(target)
    finally:
        document.close()
    return target


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Prevod PDF cenníka cez Docling")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--strany", help="rozsah strán, napr. 3-5")
    parser.add_argument("--rezim", choices=["accurate", "fast"], default="accurate")
    parser.add_argument("--ocr", choices=["vyp", "auto", "cela"], default="vyp", help="zdroj textu, pozri build_converter")
    parser.add_argument("--ako-obrazok", action="store_true",
                        help="jednu stranu (--strany N) najprv vykresliť do PNG a previesť obrázok, zapne OCR")
    args = parser.parse_args()

    suffix = "-obrazok" if args.ako_obrazok else ("" if args.ocr == "vyp" else f"-ocr-{args.ocr}")
    out_dir = OUTPUT_ROOT / (args.pdf.stem + suffix)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    if args.ako_obrazok:
        first, last = parse_pages(args.strany)
        if first != last:
            parser.error("--ako-obrazok spracuje jednu stranu, zadaj --strany N")
        image = render_page(args.pdf, first, out_dir / f"strana-{first}.png")
        result = build_converter(args.rezim, "cela").convert(image)
    else:
        result = build_converter(args.rezim, args.ocr).convert(args.pdf, page_range=parse_pages(args.strany))
    elapsed = time.perf_counter() - started
    doc = result.document
    (out_dir / "dokument.md").write_text(doc.export_to_markdown(), encoding="utf-8")
    (out_dir / "dokument.json").write_text(
        json.dumps(doc.export_to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    # Čo rozpoznal layout model: nadpisy, text, tabuľky, obrázky …
    labels = Counter(str(item.label.value) for item, _ in doc.iterate_items())
    print(f"Súbor: {args.pdf}")
    print(f"Stav: {result.status.value}, strán: {doc.num_pages()}, čas: {elapsed:.1f} s")
    print("Prvky podľa typu:", ", ".join(f"{label} {count}" for label, count in labels.most_common()))

    for index, table in enumerate(doc.tables, start=1):
        frame = table.export_to_dataframe(doc=doc)
        frame.to_csv(out_dir / f"tabulka-{index:02d}.csv", index=False, encoding="utf-8-sig")
        page = table.prov[0].page_no if table.prov else "?"
        print(f"\n=== Tabuľka {index} (strana {page}), {frame.shape[0]} riadkov × {frame.shape[1]} stĺpcov")
        print("Stĺpce:", " | ".join(str(column) for column in frame.columns))
        print(frame.head(6).to_string(max_colwidth=28))

    print(f"\nVýstup: {out_dir}")


if __name__ == "__main__":
    main()
