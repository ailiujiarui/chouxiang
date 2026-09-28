from refactor_agent.ast_analyzer import controlled_subtree_rewrite, select_target_regions





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
