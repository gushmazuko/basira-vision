import pytest

from basira_vision.processing.pdf_converter import parse_page_range


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, [1, 2, 3, 4, 5]),
        ("2", [2]),
        ("2-4", [2, 3, 4]),
        (" 4-9 ", [4, 5]),  # end clamped to the document
    ],
)
def test_valid_ranges(value, expected):
    assert parse_page_range(value, 5) == expected


@pytest.mark.parametrize("value", ["0", "0-3", "4-2", "abc", "1-x", "6", "6-8", "-3"])
def test_invalid_ranges(value):
    with pytest.raises(ValueError):
        parse_page_range(value, 5)
