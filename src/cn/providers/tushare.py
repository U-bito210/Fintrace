"""Budget-guarded Tushare Pro adapter for canonical A-share data.

Online access is disabled by default.  Callers must explicitly grant a small
request budget for an approved operation; the adapter never retries by itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Optional

from ..domain import CompanyProfile, FinancialStatement, MarketBar
from ..errors import AuthenticationError, EmptyDataError, PermissionError
from ..symbols import CanonicalSymbol
from .base import FinancialDataProvider
from .tushare_normalizer import normalize_bars, normalize_profile, normalize_statements


@dataclass
class TushareCallBudget:
    """A local, auditable cap on real provider calls (not Tushare points)."""

    max_calls: int = 0
    used_calls: int = 0

    def consume(self, endpoint: str) -> None:
        if self.used_calls >= self.max_calls:
            raise PermissionError(
                f"Tushare call budget exhausted before '{endpoint}' "
                f"({self.used_calls}/{self.max_calls})."
            )
        self.used_calls += 1


class TushareProvider(FinancialDataProvider):
    """Tushare adapter with explicit call budgeting and no hidden retries."""

    name = "tushare"

    def __init__(self, token: str, *, call_budget: Optional[TushareCallBudget] = None):
        if not token:
            raise AuthenticationError("TUSHARE_TOKEN is required for TushareProvider.")
        self._token = token
        self.call_budget = call_budget or TushareCallBudget()
        self._client: Any = None

    @classmethod
    def from_environment(cls, *, max_calls: int = 0) -> "TushareProvider":
        return cls(
            os.getenv("TUSHARE_TOKEN", "").strip(),
            call_budget=TushareCallBudget(max_calls=max_calls),
        )

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import tushare as ts
            except ImportError as exc:
                raise AuthenticationError("Install the optional 'tushare' dependency first.") from exc
            self._client = ts.pro_api(self._token)
        return self._client

    def _call(self, endpoint: str, **params: Any) -> Any:
        self.call_budget.consume(endpoint)
        frame = getattr(self._get_client(), endpoint)(**params)
        if frame is None or frame.empty:
            raise EmptyDataError(f"Tushare '{endpoint}' returned no data.")
        return frame

    def get_profile(self, symbol: CanonicalSymbol) -> Optional[CompanyProfile]:
        frame = self._call(
            "stock_basic",
            ts_code=str(symbol),
            fields="ts_code,symbol,name,area,industry,market,list_date",
        )
        return normalize_profile(frame.iloc[0], symbol)

    def get_daily_bars(
        self,
        symbol: CanonicalSymbol,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjustment: str = "RAW",
    ) -> List[MarketBar]:
        if adjustment != "RAW":
            raise PermissionError("TushareProvider v1 only exposes RAW bars; use a dedicated adj-factor path.")
        frame = self._call(
            "daily",
            ts_code=str(symbol),
            start_date=(start_date or "").replace("-", ""),
            end_date=(end_date or "").replace("-", ""),
        )
        return normalize_bars(frame, symbol)

    def get_financial_statements(
        self, symbol: CanonicalSymbol, *, research_as_of: Optional[str] = None
    ) -> List[FinancialStatement]:
        frames = {
            "income": self._call("income", ts_code=str(symbol)),
            "balance": self._call("balancesheet", ts_code=str(symbol)),
            "cashflow": self._call("cashflow", ts_code=str(symbol)),
        }
        return normalize_statements(frames, research_as_of=research_as_of)
