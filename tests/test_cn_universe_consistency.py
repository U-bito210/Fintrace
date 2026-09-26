"""Guard the curated universe and the peer (Shenwan L1) mapping together.

Adding a stock is two files, not one: the symbol must enter
``scripts/build_cn_universe.py`` *and* ``src.cn.industries._STOCK_INDUSTRY``.
If only the first happens, the snapshot is collected but ``industry_peers()``
returns ``[]`` and peer valuation silently reports "no compatible peers" — a
looks-fine-but-useless state.  These tests make that mismatch fail loudly.

They are offline: ``scripts.build_cn_universe`` only imports stdlib at module
level, so no provider call or token is involved.
"""

from __future__ import annotations

import collections

from scripts.build_cn_universe import CURATED_UNIVERSE
from src.cn.industries import (
    L1_INDUSTRIES,
    industry_peers,
    is_financial_institution,
    known_industries,
    known_stocks,
)
from src.cn.symbols import normalize_cn_symbol

# ``validate_peer_valuation`` warns below 4 peers, so an industry needs at least
# 5 mapped stocks before its P25-P75 interval means anything.
MIN_STOCKS_PER_INDUSTRY = 5


def _universe_codes() -> set[str]:
    return {symbol.split(".")[0] for symbol in CURATED_UNIVERSE}


def test_universe_has_no_duplicates():
    duplicates = [code for code, count in collections.Counter(CURATED_UNIVERSE).items() if count > 1]
    assert duplicates == []


def test_universe_and_industry_mapping_cover_the_same_codes():
    assert _universe_codes() == set(known_stocks())


def test_every_universe_symbol_has_an_industry():
    missing = sorted(_universe_codes() - set(known_stocks()))
    assert missing == [], f"collected but unclassified (would get zero peers): {missing}"


def test_every_classified_symbol_is_collected():
    uncollected = sorted(set(known_stocks()) - _universe_codes())
    assert uncollected == [], f"classified but never collected: {uncollected}"


def test_every_mapped_industry_is_a_known_l1_code():
    unknown = sorted(set(known_stocks().values()) - set(L1_INDUSTRIES))
    assert unknown == []


def test_every_covered_industry_has_enough_peers():
    counts = collections.Counter(known_stocks().values())
    thin = {code: count for code, count in sorted(counts.items()) if count < MIN_STOCKS_PER_INDUSTRY}
    assert thin == {}, f"industries with too few peers for a P25-P75 interval: {thin}"


def test_every_symbol_builds_a_non_empty_peer_group():
    empty = [symbol for symbol in CURATED_UNIVERSE if not industry_peers(symbol.split(".")[0])]
    assert empty == []


def test_peer_group_is_same_industry_and_excludes_the_target():
    for symbol in CURATED_UNIVERSE:
        code = symbol.split(".")[0]
        industry = known_stocks()[code]
        peers = industry_peers(code)
        assert code not in peers
        for peer in peers:
            assert known_stocks()[peer] == industry


def test_universe_symbols_are_canonical():
    for symbol in CURATED_UNIVERSE:
        assert normalize_cn_symbol(symbol).value == symbol


def test_industry_lookup_is_available_for_every_symbol():
    # Mirrors what the workbench does: unknown codes degrade to "未覆盖".
    assert known_industries() == L1_INDUSTRIES
    for symbol in CURATED_UNIVERSE:
        assert is_financial_institution(symbol.split(".")[0]) in (True, False)
