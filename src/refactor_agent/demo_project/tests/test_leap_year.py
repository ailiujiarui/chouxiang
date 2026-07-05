from leap_year import is_leap_year


def test_leap_year_rules():
    assert is_leap_year(2000) is True
    assert is_leap_year(2024) is True
    assert is_leap_year(1900) is False
    assert is_leap_year(2023) is False
