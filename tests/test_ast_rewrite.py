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


def test_controlled_subtree_rewrite_rejects_syntax_error():
    result = controlled_subtree_rewrite(
        "def hot(value):\n    return value\n",
        "def hot(value):\n    return\n        nope\n",
        ["hot"],
    )
    assert result.ok is False
    assert result.findings[0].rule == "candidate-syntax"


def test_select_target_regions_uses_highest_complexity_fallback():
    source = "def simple(value):\n    return value + 1\n\ndef branch(value):\n    if value:\n        return 1\n    return 0\n"
    assert select_target_regions(source) == ["branch"]
