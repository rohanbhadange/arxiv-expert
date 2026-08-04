from arxiv_expert.categories import CATEGORIES, require_category, search_categories


def test_catalog_is_large_unique_and_contains_robotics_and_rl_topics():
    codes = [category.code for category in CATEGORIES]
    assert len(codes) == 155
    assert len(codes) == len(set(codes))
    assert require_category("cs.RO").name == "Robotics"
    assert require_category("cs.LG").name == "Machine Learning"


def test_category_search_matches_codes_and_names():
    assert [item.code for item in search_categories("robotics")] == ["cs.RO"]
    assert require_category("cs.RO") in search_categories("cs.r")
