from bel.domain.normalize import normalize_counterparty, same_counterparty


def test_pdf_line_wrap_artifact_is_removed_not_spaced():
    # Spec section 15's own example shape: a PDF wraps the company name
    # across two lines mid-word. The line break must vanish, not become
    # a space. The name below is independently synthetic.
    wrapped = "金华市某某区示例体育用品" + chr(10) + "厂"
    assert normalize_counterparty(wrapped) == "金华市某某区示例体育用品厂"


def test_internal_whitespace_collapses_to_single_space():
    assert normalize_counterparty("  ABC   Trading   Co.  ") == "ABC Trading Co."


def test_nfkc_normalizes_compatibility_forms():
    # Fullwidth 'Ａ' (U+FF21) should NFKC-fold to ASCII 'A'.
    assert normalize_counterparty("ＡBC") == "ABC"


def test_none_stays_none():
    assert normalize_counterparty(None) is None


def test_entity_suffix_difference_is_not_considered_the_same():
    # Forbidden: alias inference / fuzzy matching. A short name and its
    # full legal-entity name are NOT the same counterparty unless they
    # are exactly equal after normalization. See spec section 15.
    assert same_counterparty("浦江某某家纺", "浦江某某家纺有限公司") is False


def test_identical_after_normalization_is_the_same():
    assert same_counterparty(" 浦江某某家纺有限公司 ", "浦江某某家纺有限公司") is True


def test_same_counterparty_false_when_either_side_is_none():
    assert same_counterparty(None, "ABC") is False
    assert same_counterparty("ABC", None) is False
