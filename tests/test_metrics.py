from refactor_agent.metrics import analyze_source


def test_metrics_for_nested_function():
    source = """
def f(value):
    if value > 10:
        return "big"
    if value > 0:
        return "small"
    return "zero"
"""
    metrics = analyze_source(source)
    assert metrics.loc == 6
    assert metrics.cyclomatic_complexity == 3
    assert metrics.details[0]["name"] == "f"


def test_metrics_for_empty_file():
    metrics = analyze_source("\n# comment only\n")
    assert metrics.loc == 0
    assert metrics.cyclomatic_complexity == 0
    assert metrics.details == []
