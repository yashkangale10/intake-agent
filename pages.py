"""Turn uploaded files into per-page PNG images that a multimodal LLM can read.

No OCR happens here: this module only normalises pixels (rotate by EXIF, cap resolution, one PNG per page).
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict

from PIL import Image, ImageOps, ImageSequence

STORE_DIR = Path(os.getenv("INTAKE_STORE", "./store"))
MAX_SIDE_PX = 2000          # cap longest side: keeps text legible while bounding image-token cost
PDF_DPI = 150
MAX_PAGES_PER_FILE = 20     # reject absurd uploads before paying for them
SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


class PageRef(TypedDict):
    file_id: str
    page: int      # 1-based
    path: str      # PNG on disk (state stays small; bytes are loaded only when calling the model)
    mime: str


def _save_normalised(img: Image.Image, dest: Path) -> None:
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    img.save(dest, format="PNG")


def render_pages(src: Path, file_id: str) -> list[PageRef]:
    pages_dir = STORE_DIR / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    out: list[PageRef] = []

    def add(img: Image.Image, n: int) -> None:
        dest = pages_dir / f"{file_id}_p{n}.png"
        _save_normalised(img, dest)
        out.append(PageRef(file_id=file_id, page=n, path=str(dest), mime="image/png"))

    if src.suffix.lower() == ".pdf":
        if not shutil.which("pdftoppm"):
            raise RuntimeError("pdftoppm (poppler-utils) is required to rasterise PDFs")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                subprocess.run(["pdftoppm", "-r", str(PDF_DPI), "-png", "-l", str(MAX_PAGES_PER_FILE + 1), str(src), f"{tmp}/p"], check=True, timeout=120)
            except subprocess.TimeoutExpired:
                raise ValueError(f"{src.name}: PDF rendering timed out (possibly corrupt)")
            files = sorted(Path(tmp).glob("p*.png"))
            if len(files) > MAX_PAGES_PER_FILE:
                raise ValueError(f"{src.name}: more than {MAX_PAGES_PER_FILE} pages")
            for n, f in enumerate(files, start=1):
                with Image.open(f) as im:
                    add(im, n)
    else:
        with Image.open(src) as im:
            frames = list(ImageSequence.Iterator(im))[: MAX_PAGES_PER_FILE + 1]  # multi-frame TIFF
            if len(frames) > MAX_PAGES_PER_FILE:
                raise ValueError(f"{src.name}: more than {MAX_PAGES_PER_FILE} pages")
            for n, frame in enumerate(frames, start=1):
                add(frame.copy(), n)
    return out


def png_b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()
