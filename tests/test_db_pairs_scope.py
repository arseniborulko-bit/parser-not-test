"""asins_from_pairs с частичной областью сбора: наши/конкуренты/всё. Без настоящей базы."""

from db_pairs import asins_from_pairs

A, B, C, D = "B0OUR00001", "B0OUR00002", "B0COMP0001", "B0COMP0002"


def pair(our, comp):
    return {"marketplace": "US", "our_asin": our, "our_product": "", "competitor": "", "comp_asin": comp}


PAIRS = [pair(A, C), pair(A, D), pair(B, C)]


def test_scope_all_returns_every_asin_deduplicated():
    assert set(asins_from_pairs(PAIRS)) == {A, B, C, D}
    assert set(asins_from_pairs(PAIRS, scope="all")) == {A, B, C, D}


def test_scope_ours_returns_only_our_asins():
    assert set(asins_from_pairs(PAIRS, scope="ours")) == {A, B}


def test_scope_competitors_returns_only_competitor_asins():
    assert set(asins_from_pairs(PAIRS, scope="competitors")) == {C, D}


def test_an_asin_that_is_ours_in_one_pair_counts_as_ours_even_if_it_is_a_competitor_elsewhere():
    shared, only_comp, other_our = "B0SHARED01", "B0COMP0009", "B0OUR00009"
    mixed = [pair(shared, only_comp), pair(other_our, shared)]  # shared — наш в первой паре, конкурент во второй
    assert set(asins_from_pairs(mixed, scope="ours")) == {shared, other_our}
    assert set(asins_from_pairs(mixed, scope="competitors")) == {only_comp}


def test_an_unknown_scope_falls_back_to_all():
    assert set(asins_from_pairs(PAIRS, scope="bogus")) == {A, B, C, D}


def test_ours_and_competitors_together_cover_everything_with_no_overlap_for_a_clean_portfolio():
    ours = set(asins_from_pairs(PAIRS, scope="ours"))
    competitors = set(asins_from_pairs(PAIRS, scope="competitors"))
    assert ours | competitors == {A, B, C, D}
    assert ours & competitors == set()
