from refactor_agent.ast_analyzer import controlled_subtree_rewrite, select_target_regions


def test_controlled_subtree_rewrite_preserves_non_target_text():
    original = "import math\n\n# keep me\ndef hot(value):\n    if value:\n        return 1\n    return 0\n\ndef untouched():\n    return math.pi\n"
    candidate = "import math\n\ndef hot(value):\n    return int(bool(value))\n\ndef untouched():\n    return math.e\n"
    result = controlled_subtree_rewrite(original, candidate, ["hot"])
    assert result.ok is False
    assert any(item.rule == "non-target-changed" for item in result.findings)

    candidate = "import math\n\ndef hot(value):\n    return int(bool(value))\n\ndef untouched():\n    return math.pi\n"
    result = controlled_subtree_rewrite(original, candidate, ["hot"])
    assert result.ok is True
    assert "# keep me" in result.source
    assert "return int(bool(value))" in result.source
    assert "def untouched():\n    return math.pi" in result.source


def test_controlled_subtree_rewrite_supports_class_method():
    original = "class Rules:\n    def decide(self, value):\n        if value > 0:\n            return True\n        return False\n\n    def label(self):\n        return 'rules'\n"
    candidate = "class Rules:\n    def decide(self, value):\n        return value > 0\n\n    def label(self):\n        return 'rules'\n"
    result = controlled_subtree_rewrite(original, candidate, ["Rules.decide"])
    assert result.ok is True
    assert "    def decide(self, value):\n        return value > 0" in result.source
    assert "    def label(self):" in result.source


def test_controlled_subtree_rewrite_rejects_boundary_changes():
    original = "def hot(value):\n    return value\n"
    candidates = [
        "def hot(value, fallback=None):\n    return value\n",
        "import os\n\ndef hot(value):\n    return value\n",
        "def hot(value):\n    return value\n\ndef extra():\n    return 1\n",
    ]
    for candidate in candidates:
        result = controlled_subtree_rewrite(original, candidate, ["hot"])
        assert result.ok is False


def test_controlled_subtree_rewrite_treats_empty_allowlist_as_deny_all():
    result = controlled_subtree_rewrite(
        "def hot(value):\n    return value\n",
        "def hot(value):\n    return value + 1\n",
        [],
    )

    assert result.ok is False
    assert result.allowed_regions == []
    assert any(item.rule == "non-target-changed" for item in result.findings)


def test_controlled_subtree_rewrite_rejects_syntax_error():
    result = controlled_subtree_rewrite(
        "def hot(value):\n    return value\n",
        "def hot(value):\n    return\n        nope\n",
        ["hot"],
    )
    assert result.ok is False
    assert result.findings[0].rule == "candidate-syntax"
    assert result.allowed_regions == ["hot"]


def test_select_target_regions_uses_highest_complexity_fallback():
    source = "def simple(value):\n    return value + 1\n\ndef branch(value):\n    if value:\n        return 1\n    return 0\n"
    assert [region.qualified_name for region in select_target_regions(source)] == ["branch"]


def test_select_target_regions_prefers_issue_symbol_over_complexity():
    source = (
        "def simple_bug(value):\n    return value + 1\n\n"
        "def complicated(value):\n    if value > 2:\n        return 2\n    if value > 1:\n        return 1\n    return 0\n"
    )
    regions = select_target_regions(source, "simple_bug returns the wrong value")
    assert [region.qualified_name for region in regions] == ["simple_bug"]
    assert "symbol" in regions[0].reason


def test_select_target_regions_uses_traceback_line_for_class_method():
    source = (
        "class Rules:\n"
        "    def simple_bug(self, value):\n"
        "        return value + 1\n\n"
        "def complicated(value):\n"
        "    if value > 2:\n"
        "        return 2\n"
        "    if value > 1:\n"
        "        return 1\n"
        "    return 0\n"
    )
    regions = select_target_regions(source, "Traceback: rules.py, line 3")
    assert [region.qualified_name for region in regions] == ["Rules.simple_bug"]
    assert regions[0].kind == "method"
    assert "traceback line" in regions[0].reason


def test_select_target_regions_supports_explicit_module_statement_line():
    source = "LIMIT = 10\n\ndef unchanged():\n    return 1\n"
    regions = select_target_regions(source, "Fix config.py:1")
    assert [region.qualified_name for region in regions] == ["module:1:Assign"]
    assert regions[0].kind == "module"


def test_select_target_regions_does_not_guess_module_only_file():
    assert select_target_regions("LIMIT = 10\n", "wrong value") == []


def test_controlled_subtree_rewrite_replaces_only_explicit_module_statement():
    original = "# keep\nLIMIT = 10\nMODE = 'safe'\n"
    candidate = "LIMIT = 20\nMODE = 'safe'\n"
    region = select_target_regions(original, "config.py:2")

    result = controlled_subtree_rewrite(original, candidate, region)

    assert result.ok is True
    assert result.changed_regions == ["module:2:Assign"]
    assert result.source == "# keep\nLIMIT = 20\nMODE = 'safe'\n"


def test_controlled_subtree_rewrite_rejects_non_target_module_change():
    original = "LIMIT = 10\nMODE = 'safe'\n"
    candidate = "LIMIT = 20\nMODE = 'unsafe'\n"

    result = controlled_subtree_rewrite(original, candidate, ["module:1:Assign"])

    assert result.ok is False
    assert any(item.rule == "module-boundary-changed" for item in result.findings)


def test_controlled_subtree_rewrite_rejects_module_binding_rename():
    result = controlled_subtree_rewrite(
        "LIMIT = 10\n",
        "OTHER = 20\n",
        ["module:1:Assign"],
    )

    assert result.ok is False
    assert any(item.rule == "module-target-binding-changed" for item in result.findings)


def test_controlled_subtree_rewrite_allows_only_explicit_import_roots():
    original = "def area(radius):\n    return radius * radius\n"
    candidate = "import math\n\ndef area(radius):\n    return math.pi * radius * radius\n"
    denied = controlled_subtree_rewrite(original, candidate, ["area"])
    assert denied.ok is False
    assert any(item.rule == "import-not-allowlisted" for item in denied.findings)
    accepted = controlled_subtree_rewrite(original, candidate, ["area"], {"math"})
    assert accepted.ok is True
    assert accepted.added_imports == ["import math"]
    assert accepted.source.startswith("import math\n")


def test_controlled_subtree_rewrite_rejects_unsafe_import_variants():
    original = "import math\n\ndef area(radius):\n    return radius * radius\n"
    candidates = [
        "def area(radius):\n    return radius * radius\n",
        "import math\nfrom .helpers import area_value\n\ndef area(radius):\n    return area_value(radius)\n",
        "import math\nfrom helpers import *\n\ndef area(radius):\n    return area(radius)\n",
        "import math\nimport subprocess\n\ndef area(radius):\n    return subprocess.call([])\n",
    ]
    for candidate in candidates:
        result = controlled_subtree_rewrite(original, candidate, ["area"], {"helpers", "subprocess"})
        assert result.ok is False


def test_controlled_subtree_rewrite_controls_function_local_imports():
    original = "def area(radius):\n    return radius * radius\n"
    candidate = "def area(radius):\n    import math\n    return math.pi * radius * radius\n"

    denied = controlled_subtree_rewrite(original, candidate, ["area"])
    accepted = controlled_subtree_rewrite(original, candidate, ["area"], {"math"})

    assert denied.ok is False
    assert any(item.rule == "import-not-allowlisted" for item in denied.findings)
    assert accepted.ok is True
    assert accepted.added_imports == ["import math"]
    assert accepted.source.count("import math") == 1


def test_controlled_subtree_rewrite_rejects_moving_existing_import_into_target():
    original = "import math\n\ndef area(radius):\n    return math.pi * radius * radius\n"
    candidate = "def area(radius):\n    import math\n    return math.pi * radius * radius\n"

    result = controlled_subtree_rewrite(original, candidate, ["area"], {"math"})

    assert result.ok is False
    assert any(item.rule == "import-removed-or-changed" for item in result.findings)


def test_select_target_regions_does_not_treat_complexity_as_issue_evidence():
    branches = "".join(f"    if value == {index}:\n        return {index}\n" for index in range(105))
    source = f"def first(value):\n{branches}    return -1\n\n" f"def second(value):\n{branches}    return -1\n"

    regions = select_target_regions(source)

    assert len(regions) == 1
