from refactor_agent.ast_analyzer import analyze_ast, ast_prompt_summary, validate_candidate_source


def test_analyze_ast_extracts_functions_and_complexity():
    source = """
def score(value):
    if value > 10:
        return "big"
    if value > 0:
        return "small"
    return "zero"
"""
    analysis = analyze_ast(source)
    assert analysis.loc == 6
    assert analysis.cyclomatic_complexity == 3
    assert analysis.functions[0].name == "score"
    assert analysis.functions[0].args == ["value"]
    assert "score" in analysis.public_symbols
    assert "AST CC=3" in ast_prompt_summary(analysis)


def test_validate_candidate_rejects_removed_public_api():
    result = validate_candidate_source(
        "def public(value):\n    return value\n",
        "def renamed(value):\n    return value\n",
    )
    assert result.ok is False
    assert result.findings[0].rule == "public-api-removed"


def test_validate_candidate_rejects_dangerous_calls():
    result = validate_candidate_source(
        "def public(value):\n    return value\n",
        "def public(value):\n    return eval(value)\n",
    )
    assert result.ok is False
    assert result.findings[0].rule == "blocked-call"


def test_validate_candidate_rejects_syntax_error():
    result = validate_candidate_source(
        "def public(value):\n    return value\n",
        "def public(value):\n    return\n        nope\n",
    )
    assert result.ok is False
    assert result.findings[0].rule == "candidate-syntax"
