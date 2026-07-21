"""Build the ChronoGate SOP PDF from Markdown + generated figures.

    PYTHONPATH=. .venv/bin/python docs/sop/build.py            # figures + PDF
    PYTHONPATH=. .venv/bin/python docs/sop/build.py --no-figures

Pipeline: (optionally) regenerate the annotated screenshots and diagrams, then
render ``sop.md`` -> HTML (python-markdown) -> ``ChronoGate-SOP.pdf`` (WeasyPrint)
with a cover page, table of contents and page numbers from ``style.css``.

Build-time dependencies are the optional ``docs`` extra (kept out of the app so
installers never bundle them):  ``pip install -e ".[docs]"``.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "Agg")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

OUT = HERE / "ChronoGate-SOP.pdf"


def _version() -> str:
    ns: dict = {}
    exec((ROOT / "chronogate" / "__init__.py").read_text().split("__all__")[0], ns)
    return ns.get("__version__", "0.0.0")


def _cover(version: str, date: str) -> str:
    return (
        '<div class="cover">'
        "<h1>ChronoGate</h1>"
        '<div class="accent"></div>'
        '<p class="tagline">Standard Operating Procedure &mdash; '
        "interactive time-gating, lifetime &amp; phasor FLIM analysis</p>"
        f'<p class="meta">Version {version}<br>{date}<br>'
        "PicoQuant .ptu &middot; Becker &amp; Hickl .sdt</p>"
        "</div>"
    )


def build(regen_figures: bool = True) -> Path:
    try:
        import markdown
        from weasyprint import HTML
    except ImportError as exc:
        sys.exit(f"missing build dependency ({exc}); install with: "
                 'pip install -e ".[docs]"')

    if regen_figures:
        from docs.sop import diagrams, figures
        print("diagrams:", ", ".join(diagrams.build_all()))
        print("figures:", ", ".join(figures.build_all()))

    version = _version()
    date = datetime.date.today().isoformat()
    md_text = (HERE / "sop.md").read_text()
    md_text = md_text.replace("{VERSION}", version).replace("{DATE}", date)

    html_body = markdown.markdown(
        md_text,
        extensions=["toc", "tables", "fenced_code", "attr_list", "sane_lists"],
    )
    css = (HERE / "style.css").read_text()
    document = (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{css}</style></head><body>"
        f"{_cover(version, date)}{html_body}</body></html>"
    )

    # base_url = HERE so ``img/*.png`` resolve relative to this folder.
    HTML(string=document, base_url=str(HERE)).write_pdf(str(OUT))
    print(f"wrote {OUT}  ({OUT.stat().st_size // 1024} KB)")
    return OUT


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-figures", action="store_true",
                    help="use the committed PNGs; skip regenerating figures/diagrams")
    args = ap.parse_args()
    build(regen_figures=not args.no_figures)
