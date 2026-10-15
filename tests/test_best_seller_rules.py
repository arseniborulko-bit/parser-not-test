"""Правила К1–К4 и приоритет подбора подкатегорий Best Seller (SOP-AMZ-001). Числа — из макета вкладки:
листинг B0GNBSDL65, целевой BSR 60 000 (верх диапазона 55 000 – 60 000 из SOP)."""

import pytest

import best_seller as bs

TARGET = 60_000


def test_mockup_relevant_subcategory_passes_all_criteria_with_high_priority():
    v = bs.evaluate(TARGET, 460_779, item_count=100, relevance=bs.RELEVANT, leader_badge=True)
    assert (v.k1, v.k2, v.k3, v.k4) == (True, True, True, True)
    assert v.status == bs.SUITABLE and v.priority == bs.HIGH
    assert round(v.leader_to_target, 1) == 7.7


def test_mockup_semi_relevant_subcategory_gets_low_priority():
    v = bs.evaluate(TARGET, 77_590, item_count=100, relevance=bs.SEMI_RELEVANT, leader_badge=True)
    assert v.status == bs.SUITABLE and v.priority == bs.LOW
    assert round(v.leader_to_target, 1) == 1.3


@pytest.mark.parametrize("leader_bsr", [35_598, 358, 324])
def test_mockup_strong_leaders_are_rejected_by_k1_without_checking_the_rest(leader_bsr):
    v = bs.evaluate(TARGET, leader_bsr, item_count=100, relevance=bs.RELEVANT, leader_badge=True)
    assert (v.k1, v.k2, v.k3, v.k4) == (False, None, None, None)
    assert v.status == bs.REJECTED and v.priority is None and v.reason == "отклонена по К1"


def test_k1_needs_both_the_floor_and_a_leader_weaker_than_our_target():
    # Лидер слабее цели, но сильнее порога 50 000 — не проходит.
    assert bs.evaluate(40_000, 45_000).k1 is False
    # Порог пройден, но лидер не слабее цели — не проходит.
    assert bs.evaluate(TARGET, TARGET).k1 is False
    assert bs.evaluate(TARGET, TARGET + 1).k1 is True
    assert bs.evaluate(10_000, bs.K1_MIN_LEADER_BSR).k1 is True


def test_k2_requires_a_full_top_of_one_hundred():
    assert bs.evaluate(TARGET, 100_000, item_count=99).reason == "отклонена по К2"
    assert bs.evaluate(TARGET, 100_000, item_count=100).k2 is True


def test_irrelevant_subcategory_is_rejected_by_k3():
    v = bs.evaluate(TARGET, 100_000, item_count=100, relevance=bs.IRRELEVANT, leader_badge=True)
    assert (v.k3, v.k4, v.status) == (False, None, bs.REJECTED)


def test_risky_subcategory_is_suitable_but_marked_to_keep_searching():
    v = bs.evaluate(TARGET, 100_000, item_count=100, relevance=bs.RISKY, leader_badge=True)
    assert v.status == bs.SUITABLE and v.priority == bs.RISK


def test_leader_without_a_live_badge_is_rejected_by_k4():
    v = bs.evaluate(TARGET, 100_000, item_count=100, relevance=bs.RELEVANT, leader_badge=False)
    assert (v.k4, v.status, v.reason) == (False, bs.REJECTED, "отклонена по К4")


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"leader_bsr": None}, "нужен BSR лидера"),
        ({"leader_bsr": 100_000}, "нужно число товаров в подкатегории"),
        ({"leader_bsr": 100_000, "item_count": 100}, "нужна оценка релевантности"),
        ({"leader_bsr": 100_000, "item_count": 100, "relevance": bs.RELEVANT}, "нужно проверить бейдж лидера"),
    ],
)
def test_missing_data_for_the_next_criterion_is_pending_not_rejected(kwargs, reason):
    v = bs.evaluate(TARGET, **kwargs)
    assert v.status == bs.PENDING and v.reason == reason and v.priority is None


def test_invalid_inputs_are_refused():
    with pytest.raises(ValueError):
        bs.evaluate(0, 100_000)
    with pytest.raises(ValueError):
        bs.evaluate(TARGET, 100_000, item_count=100, relevance="maybe")


@pytest.mark.parametrize(
    "ranks, needed",
    [
        ([166, 191], True),  # макет: #166 и #191 в своих подкатегориях — ищем
        ([3, 250], False),  # макет: B0DG2B7KJN уже #3 — поиск не нужен
        ([1], False),
        ([4, None], True),
        ([], True),
    ],
)
def test_search_is_skipped_when_already_in_a_top_three(ranks, needed):
    assert bs.search_needed(ranks) is needed


def test_ranking_puts_high_priority_first_then_low_then_pending_then_rejected():
    rejected = bs.evaluate(TARGET, 358)
    low = bs.evaluate(TARGET, 77_590, 100, bs.SEMI_RELEVANT, True)
    high_weak_leader = bs.evaluate(TARGET, 460_779, 100, bs.RELEVANT, True)
    high_stronger_leader = bs.evaluate(TARGET, 90_000, 100, bs.RELEVANT, True)
    pending = bs.evaluate(TARGET, 200_000)
    order = bs.ranked([rejected, pending, low, high_stronger_leader, high_weak_leader])
    assert order == [high_weak_leader, high_stronger_leader, low, pending, rejected]
