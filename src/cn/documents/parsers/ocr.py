"""Local OCR fallback for scanned PDF pages.

The parser deliberately uses only an OCR engine installed on the local host.
It never sends a filing to a hosted OCR service.  A successful OCR pass yields
verbatim page text for review; it does not turn uncertain OCR output into a
normalised financial value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import OCRUnavailable, PDFPLUMBER_AVAILABLE


class OCRFallback:
    name = "ocr_fallback"
    version = "1.1.0"

    def __init__(self) -> None:
        self.backend = self._detect_backend()

    @staticmethod
    def _detect_backend() -> str | None:
        # pytesseract is first because it is a small adapter around a locally
        # installed Tesseract executable. PaddleOCR remains supported for
        # deployments that already use it.
        for module, label in (("pytesseract", "tesseract"), ("paddleocr", "paddleocr")):
            try:
                __import__(module)  # pragma: no cover - depends on install
                if label == "tesseract":
                    # Importing pytesseract alone is insufficient: without its
                    # native executable every scan would fail at request time.
                    import pytesseract  # type: ignore[import-not-found]
                    _configure_tesseract(pytesseract)
                    pytesseract.get_tesseract_version()
                return label
            except Exception:
                continue
        return None

    def available(self) -> bool:
        return self.backend is not None

    def ocr_page(self, page) -> str:
        """OCR one pdfplumber page. Returns text, or raises ``OCRUnavailable``."""
        if not PDFPLUMBER_AVAILABLE:
            raise OCRUnavailable("pdfplumber 未安装，无法读取扫描页图像。")
        if self.backend is None:
            raise OCRUnavailable(
                "未检测到可用的本地 OCR：请安装 Tesseract（含 chi_sim 语言包）和 pytesseract，"
                "或配置 PaddleOCR。"
            )

        image = page.to_image(resolution=220).original.convert("RGB")
        if self.backend == "tesseract":
            return self._ocr_tesseract(image)
        if self.backend == "paddleocr":
            return self._ocr_paddle(image)
        raise OCRUnavailable(f"不支持的 OCR 后端：{self.backend}")

    @staticmethod
    def _language() -> str:
        """Host override for installations with a differently named CJK pack."""
        return os.getenv("FINTRACE_OCR_LANG", "chi_sim+eng")

    def _ocr_tesseract(self, image: Any) -> str:
        try:
            import pytesseract  # type: ignore[import-not-found]
            _configure_tesseract(pytesseract)
            text = pytesseract.image_to_string(
                image, lang=self._language(), config="--psm 6"
            )
        except Exception as exc:  # pragma: no cover - depends on host runtime
            raise OCRUnavailable(f"Tesseract 识别失败：{exc}") from exc
        return self._clean_text(text)

    def _ocr_paddle(self, image: Any) -> str:
        try:
            import numpy as np
            from paddleocr import PaddleOCR  # type: ignore[import-not-found]
            # ``ocr`` is retained across PaddleOCR 2.x releases.  Use the
            # conservative invocation so deployments can opt in without a
            # network model download being hidden in this parser.
            engine = PaddleOCR(lang="ch", use_angle_cls=True, show_log=False)
            result = engine.ocr(np.asarray(image), cls=True)
            lines = []
            for page_rows in result or []:
                for row in page_rows or []:
                    if not isinstance(row, (list, tuple)) or len(row) < 2:
                        continue
                    payload = row[1]
                    if isinstance(payload, (list, tuple)) and payload:
                        candidate = str(payload[0]).strip()
                        if candidate:
                            lines.append(candidate)
        except Exception as exc:  # pragma: no cover - depends on host runtime
            raise OCRUnavailable(f"PaddleOCR 识别失败：{exc}") from exc
        return self._clean_text("\n".join(lines))

    @staticmethod
    def _clean_text(value: object) -> str:
        """Keep OCR output verbatim apart from empty lines and terminal space."""
        return "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())


def _configure_tesseract(module: Any) -> None:
    """Find a standard Windows install without relying on a restarted PATH.

    ``TESSDATA_PREFIX`` may point at ``data/ocr/tessdata`` in this repository;
    it is useful when a language model is intentionally project-local rather
    than written into the protected Program Files directory.
    """
    executable = os.getenv("TESSERACT_CMD", "").strip()
    candidates = [
        executable,
        r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
        r"C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe",
    ]
    binding = getattr(module, "pytesseract", module)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            binding.tesseract_cmd = candidate
            break
    local_tessdata = Path(__file__).resolve().parents[4] / "data" / "ocr" / "tessdata"
    if local_tessdata.is_dir():
        os.environ.setdefault("TESSDATA_PREFIX", str(local_tessdata))
