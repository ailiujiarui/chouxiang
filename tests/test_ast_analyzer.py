from refactor_agent.ast_analyzer import (
    analyze_ast,
    ast_hotspot_prompt,
    ast_prompt_summary,
    controlled_subtree_rewrite,
    select_target_regions,
    validate_candidate_source,
)


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


def test_validate_candidate_allows_unchanged_preexisting_safety_findings():
    original = (
        "def target(value):\n"
        "    \"\"\"A deliberately longer original function.\"\"\"\n"
        "    return value\n\n"
        "def worker():\n"
        "    while True:\n"
        "        break\n"
    )
    candidate = (
        "def target(value):\n"
        "    return value + 1\n\n"
        "def worker():\n"
        "    while True:\n"
        "        break\n"
    )

    result = validate_candidate_source(original, candidate)

    assert result.ok is True


def test_validate_candidate_rejects_new_safety_finding_alongside_preexisting_one():
    original = "def target(value):\n    return value\n\ndef worker():\n    while True:\n        break\n"
    candidate = (
        "def target(value):\n"
        "    while True:\n"
        "        return value\n\n"
        "def worker():\n"
        "    while True:\n"
        "        break\n"
    )

    result = validate_candidate_source(original, candidate)

    assert result.ok is False
    assert [finding.rule for finding in result.findings] == ["infinite-loop-risk"]


def test_validate_candidate_rejects_preexisting_danger_moved_to_new_control_path():
    original = "def target(flag):\n    if flag:\n        exec('work')\n"
    candidate = "def target(flag):\n    if not flag:\n        exec('work')\n"

    result = validate_candidate_source(original, candidate)

    assert result.ok is False
    assert [finding.rule for finding in result.findings] == ["blocked-call"]


def test_validate_candidate_rejects_preexisting_danger_moved_between_duplicate_branches():
    original = (
        "def target(flag):\n"
        "    if flag:\n"
        "        exec('work')\n"
        "    if flag:\n"
        "        pass\n"
    )
    candidate = (
        "def target(flag):\n"
        "    if flag:\n"
        "        pass\n"
        "    if flag:\n"
        "        exec('work')\n"
    )

    result = validate_candidate_source(original, candidate)

    assert result.ok is False
    assert [finding.rule for finding in result.findings] == ["blocked-call"]


def test_validate_candidate_allows_safe_statement_before_unchanged_danger():
    original = "def target():\n    exec('work')\n"
    candidate = "def target():\n    import math\n    exec('work')\n"

    result = validate_candidate_source(original, candidate)

    assert result.ok is True


def test_validate_candidate_rejects_syntax_error():
    result = validate_candidate_source(
        "def public(value):\n    return value\n",
        "def public(value):\n    return\n        nope\n",
    )
    assert result.ok is False
    assert result.findings[0].rule == "candidate-syntax"


def test_ast_hotspot_prompt_extracts_high_complexity_subtree():
    source = """
def boring(value):
    return value


def messy(value):
    if value > 10:
        return "big"
    if value > 0:
        return "small"
    if value == 0:
        return "zero"
    return "negative"
"""
    prompt = ast_hotspot_prompt(source)

    assert "AST 热点子树" in prompt
    assert "`messy`" in prompt
    assert "结构熵" in prompt
    assert "def boring" not in prompt


def test_ast_hotspot_prompt_handles_simple_code():
    prompt = ast_hotspot_prompt("def tiny(value):\n    return value\n")
    assert "未发现超过复杂度阈值" in prompt
