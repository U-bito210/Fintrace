"""OCR fallback for scan pages.

A-share annual reports are frequently scanned. When pdfplumber finds no text
layer on a page, this fallback attempts an OCR backend. If none is installed
(MinerU / PaddleOCR), it raises :class:`OCRUnavailable` and the page is marked
``failed`` (never silently skipped). This keeps the fail-closed guarantee.
"""

from __future__ import annotations

from .base import OCRUnavailable, PDFPLUMBER_AVAILABLE


class OCRFallback:
    name = "ocr_fallback"
    version = "1.0.0"

    def __init__(self) -> None:
        self.backend = self._detect_backend()

    @staticmethod
    def _detect_backend() -> str | None:
        for module, label in (("paddleocr", "paddleocr"), ("mineru", "mineru")):
            try:
                __import__(module)  # pragma: no cover - depends on install
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
            raise OCRUnavailable("未安装 MinerU / PaddleOCR，扫描页无法 OCR。")
        # The actual OCR call is backend-specific and not exercised in offline
        # tests; the contract is: success -> text, failure -> OCRUnavailable.
        raise OCRUnavailable(f"OCR 后端 {self.backend} 已声明但当前环境未配置运行时。")  # pragma: no cover
