from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from math import log2
import textwrap

from refactor_agent.models import (
    AstAnalysis,
    AstRewriteResult,
    CandidateValidationResult,
    ClassSummary,
    FunctionSignature,
    SafetyFinding,
)


HIGH_COMPLEXITY_THRESHOLD = 4
MAX_HOTSPOT_SOURCE_CHARS = 3000
BLOCKED_IMPORTS = {"socket"}
BLOCKED_CALLS = {
    "__import__",
    "compile",
    "eval",
    "exec",
    "input",
    "os.remove",
    "os.rmdir",
    "os.system",
    "os.unlink",
    "pathlib.Path.unlink",
    "shutil.rmtree",
    "subprocess.call",
    "subprocess.Popen",
    "subprocess.run",
}


def analyze_ast(source: str) -> AstAnalysis:
    tree = ast.parse(source)
    functions: list[FunctionSignature] = []
    classes: list[ClassSummary] = []
    public_symbols: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = _function_signature(node, node.name)
            functions.append(signature)
            if not node.name.startswith("_"):
                public_symbols.append(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = [
                _function_signature(child, f"{node.name}.{child.name}")
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append(
                ClassSummary(
                    name=node.name,
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", None),
                    methods=methods,
                )
            )
            if not node.name.startswith("_"):
                public_symbols.append(node.name)

    high_complexity = sorted(
        [*functions, *(method for class_summary in classes for method in class_summary.methods)],
        key=lambda item: item.complexity,
        reverse=True,
    )
    high_complexity = [item for item in high_complexity if item.complexity >= HIGH_COMPLEXITY_THRESHOLD]
    return AstAnalysis(
        loc=_count_loc(source),
        cyclomatic_complexity=sum(item.complexity for item in functions)
        + sum(method.complexity for class_summary in classes for method in class_summary.methods),
        functions=functions,
        classes=classes,
        public_symbols=public_symbols,
        high_complexity_regions=high_complexity,
        safety_findings=list(_safety_findings(tree)),
    )


def validate_candidate_source(original_source: str, candidate_source: str) -> CandidateValidationResult:
    try:
        original = analyze_ast(original_source)
    except SyntaxError as exc:
        return CandidateValidationResult(
            ok=False,
            findings=[SafetyFinding(rule="original-syntax", message=str(exc), lineno=exc.lineno)],
        )

    try:
        candidate = analyze_ast(candidate_source)
    except SyntaxError as exc:
        return CandidateValidationResult(
            ok=False,
            findings=[SafetyFinding(rule="candidate-syntax", message=str(exc), lineno=exc.lineno)],
        )

    findings: list[SafetyFinding] = []
    findings.extend(finding for finding in candidate.safety_findings if finding.severity == "error")
    findings.extend(_public_api_findings(original, candidate))
    return CandidateValidationResult(ok=not findings, analysis=candidate, findings=findings)


def select_target_regions(source: str, max_regions: int = 3) -> list[str]:
    analysis = analyze_ast(source)
    functions = [
        *analysis.functions,
        *(method for class_summary in analysis.classes for method in class_summary.methods),
    ]
    ranked = sorted(functions, key=lambda item: (item.complexity, item.end_lineno or item.lineno), reverse=True)
    hotspots = [item for item in ranked if item.complexity >= HIGH_COMPLEXITY_THRESHOLD]
    selected = hotspots[:max_regions] or ranked[:1]
    return [item.qualified_name for item in selected]


def controlled_subtree_rewrite(
    original_source: str,
    candidate_source: str,
    allowed_regions: list[str] | None = None,
) -> AstRewriteResult:
    try:
        original_tree = ast.parse(original_source)
    except SyntaxError as exc:
        finding = SafetyFinding(rule="original-syntax", message=str(exc), lineno=exc.lineno)
        return AstRewriteResult(ok=False, source=original_source, findings=[finding])
    try:
        candidate_tree = ast.parse(candidate_source)
    except SyntaxError as exc:
        finding = SafetyFinding(rule="candidate-syntax", message=str(exc), lineno=exc.lineno)
        return AstRewriteResult(ok=False, source=original_source, findings=[finding])

    allowed = allowed_regions or select_target_regions(original_source)
    findings: list[SafetyFinding] = []
    original_nodes = _qualified_function_nodes(original_tree)
    candidate_nodes = _qualified_function_nodes(candidate_tree)
    changed = sorted(
        name
        for name in set(original_nodes) | set(candidate_nodes)
        if _node_fingerprint(original_nodes.get(name)) != _node_fingerprint(candidate_nodes.get(name))
    )

    findings.extend(_module_boundary_findings(original_tree, candidate_tree))
    for name in changed:
        if name not in allowed:
            node = candidate_nodes.get(name) or original_nodes.get(name)
            findings.append(
                SafetyFinding(
                    rule="non-target-changed",
                    message=f"Function or method {name!r} changed outside the allowed AST regions {allowed!r}.",
                    lineno=getattr(node, "lineno", None),
                )
            )
    for name in allowed:
        original_node = original_nodes.get(name)
        candidate_node = candidate_nodes.get(name)
        if original_node is None or candidate_node is None:
            findings.append(
                SafetyFinding(
                    rule="target-region-missing",
                    message=f"Allowed AST region {name!r} must exist in both original and candidate code.",
                    lineno=getattr(candidate_node or original_node, "lineno", None),
                )
            )
            continue
        if _signature_fingerprint(original_node) != _signature_fingerprint(candidate_node):
            findings.append(
                SafetyFinding(
                    rule="signature-changed",
                    message=f"Allowed AST region {name!r} changed its signature or decorators.",
                    lineno=candidate_node.lineno,
                )
            )

    validation = validate_candidate_source(original_source, candidate_source)
    findings.extend(validation.findings)
    findings = _deduplicate_findings(findings)
    if findings:
        return AstRewriteResult(
            ok=False,
            source=original_source,
            allowed_regions=allowed,
            changed_regions=changed,
            findings=findings,
        )

    original_lines = original_source.splitlines(keepends=True)
    candidate_lines = candidate_source.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []
    for name in changed:
        original_node = original_nodes[name]
        candidate_node = candidate_nodes[name]
        replacement = _node_source(candidate_lines, candidate_node, original_node.col_offset)
        replacements.append((original_node.lineno - 1, original_node.end_lineno or original_node.lineno, replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        original_lines[start:end] = [replacement]
    rewritten = "".join(original_lines)
    final_validation = validate_candidate_source(original_source, rewritten)
    if not final_validation.ok:
        return AstRewriteResult(
            ok=False,
            source=original_source,
            allowed_regions=allowed,
            changed_regions=changed,
            findings=final_validation.findings,
        )
    return AstRewriteResult(
        ok=True,
        source=rewritten,
        allowed_regions=allowed,
        changed_regions=changed,
    )


def ast_prompt_summary(analysis: AstAnalysis) -> str:
    functions = ", ".join(
        f"{item.qualified_name}({', '.join(item.args)}) cc={item.complexity}"
        for item in [*analysis.functions, *(method for class_summary in analysis.classes for method in class_summary.methods)]
    )
    hot_spots = ", ".join(
        f"{item.qualified_name}@{item.lineno}-{item.end_lineno or item.lineno} cc={item.complexity}"
        for item in analysis.high_complexity_regions[:5]
    )
    public_symbols = ", ".join(analysis.public_symbols) or "none"
    findings = "; ".join(f"{item.rule}@{item.lineno}: {item.message}" for item in analysis.safety_findings) or "none"
    return (
        f"AST LOC={analysis.loc}; AST CC={analysis.cyclomatic_complexity}; "
        f"Public API={public_symbols}; Functions={functions or 'none'}; "
        f"High-complexity subtrees={hot_spots or 'none'}; Safety findings={findings}"
    )


def ast_hotspot_prompt(source: str, max_regions: int = 3) -> str:
    """Render high-complexity AST subtrees as a targeted refactor prompt section."""
    analysis = analyze_ast(source)
    if not analysis.high_complexity_regions:
        return "AST 热点子树：未发现超过复杂度阈值的函数，优先保持公开 API 和测试语义。"

    tree = ast.parse(source)
    lines = source.splitlines()
    sections = ["AST 热点子树（优先重构这些区域，不要盲目重写全文件）："]
    consumed_chars = 0
    for index, region in enumerate(analysis.high_complexity_regions[:max_regions], start=1):
        snippet = _source_slice(lines, region.lineno, region.end_lineno or region.lineno)
        if consumed_chars + len(snippet) > MAX_HOTSPOT_SOURCE_CHARS:
            remaining = max(MAX_HOTSPOT_SOURCE_CHARS - consumed_chars, 0)
            snippet = snippet[:remaining].rstrip() + "\n# ... 热点源码已截断"
        consumed_chars += len(snippet)
        metrics = _subtree_metrics(tree, region.qualified_name)
        sections.extend(
            [
                (
                    f"{index}. `{region.qualified_name}` 行 {region.lineno}-{region.end_lineno or region.lineno}: "
                    f"CC={region.complexity}, AST 节点={int(metrics['nodes'])}, 结构熵={metrics['entropy']:.2f}"
                ),
                "```python",
                snippet,
                "```",
            ]
        )
        if consumed_chars >= MAX_HOTSPOT_SOURCE_CHARS:
            break
    return "\n".join(sections)


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, qualified_name: str) -> FunctionSignature:
    args = [
        *(arg.arg for arg in node.args.posonlyargs),
        *(arg.arg for arg in node.args.args),
        *(arg.arg for arg in node.args.kwonlyargs),
    ]
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    return FunctionSignature(
        name=node.name,
        qualified_name=qualified_name,
        args=args,
        lineno=node.lineno,
        end_lineno=getattr(node, "end_lineno", None),
        complexity=_complexity(node),
    )


def _complexity(node: ast.AST) -> int:
    visitor = _ComplexityVisitor()
    visitor.visit(node)
    return visitor.complexity


def _count_loc(source: str) -> int:
    return sum(1 for line in source.splitlines() if line.strip() and not line.strip().startswith("#"))


def _source_slice(lines: list[str], start_line: int, end_line: int) -> str:
    start = max(start_line - 1, 0)
    end = min(end_line, len(lines))
    return "\n".join(lines[start:end])


def _subtree_metrics(tree: ast.AST, qualified_name: str) -> dict[str, float]:
    node = _find_qualified_node(tree, qualified_name)
    if node is None:
        return {"nodes": 0.0, "entropy": 0.0}
    node_types = [type(item).__name__ for item in ast.walk(node)]
    counts = Counter(node_types)
    total = sum(counts.values())
    entropy = -sum((count / total) * log2(count / total) for count in counts.values()) if total else 0.0
    return {"nodes": float(total), "entropy": entropy}


def _find_qualified_node(tree: ast.AST, qualified_name: str) -> ast.AST | None:
    parts = qualified_name.split(".")
    if not parts:
        return None
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == qualified_name:
            return node
        if isinstance(node, ast.ClassDef) and node.name == parts[0]:
            if len(parts) == 1:
                return node
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == parts[1]:
                    return child
    return None


def _safety_findings(tree: ast.AST) -> Iterable[SafetyFinding]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = [alias.name.split(".")[0] for alias in getattr(node, "names", [])]
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".")[0])
            for name in imported:
                if name in BLOCKED_IMPORTS:
                    yield SafetyFinding(
                        rule="blocked-import",
                        message=f"Importing {name!r} is blocked in generated code.",
                        lineno=getattr(node, "lineno", None),
                    )
        elif isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name in BLOCKED_CALLS or _matches_blocked_suffix(call_name):
                yield SafetyFinding(
                    rule="blocked-call",
                    message=f"Calling {call_name!r} is blocked in generated code.",
                    lineno=getattr(node, "lineno", None),
                )
        elif isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
            yield SafetyFinding(
                rule="infinite-loop-risk",
                message="Literal while True loop is blocked before sandbox execution.",
                lineno=node.lineno,
            )


def _public_api_findings(original: AstAnalysis, candidate: AstAnalysis) -> list[SafetyFinding]:
    findings: list[SafetyFinding] = []
    candidate_symbols = set(candidate.public_symbols)
    for symbol in original.public_symbols:
        if symbol not in candidate_symbols:
            findings.append(
                SafetyFinding(
                    rule="public-api-removed",
                    message=f"Public symbol {symbol!r} from original code is missing in candidate.",
                )
            )

    original_symbols = set(original.public_symbols)
    for symbol in candidate.public_symbols:
        if symbol not in original_symbols:
            findings.append(
                SafetyFinding(
                    rule="public-api-added",
                    message=f"Candidate added public symbol {symbol!r} outside the original API.",
                )
            )

    original_functions = {
        item.qualified_name: item
        for item in [*original.functions, *(method for item in original.classes for method in item.methods)]
    }
    candidate_functions = {
        item.qualified_name: item
        for item in [*candidate.functions, *(method for item in candidate.classes for method in item.methods)]
    }
    for name, signature in original_functions.items():
        candidate_signature = candidate_functions.get(name)
        if candidate_signature and candidate_signature.args != signature.args:
            findings.append(
                SafetyFinding(
                    rule="signature-changed",
                    message=(
                        f"Function {name!r} changed args from {signature.args!r} "
                        f"to {candidate_signature.args!r}."
                    ),
                    lineno=candidate_signature.lineno,
                )
            )
    return findings


def _qualified_function_nodes(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes[f"{node.name}.{child.name}"] = child
    return nodes


def _node_fingerprint(node: ast.AST | None) -> str | None:
    return ast.dump(node, include_attributes=False) if node is not None else None


def _signature_fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.dump(_function_shell(node), include_attributes=False)


def _module_boundary_findings(original: ast.Module, candidate: ast.Module) -> list[SafetyFinding]:
    original_nodes = _qualified_function_nodes(original)
    candidate_nodes = _qualified_function_nodes(candidate)
    findings: list[SafetyFinding] = []
    if set(original_nodes) != set(candidate_nodes):
        findings.append(
            SafetyFinding(
                rule="function-set-changed",
                message="Candidate added or removed a function/method outside controlled subtree replacement.",
            )
        )
    if _non_function_fingerprint(original) != _non_function_fingerprint(candidate):
        findings.append(
            SafetyFinding(
                rule="module-boundary-changed",
                message="Imports, classes, assignments, or other non-function module structure changed.",
            )
        )
    return findings


def _non_function_fingerprint(tree: ast.Module) -> str:
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body.append(_function_shell(node))
        elif isinstance(node, ast.ClassDef):
            class_body = [
                _function_shell(child) if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else child
                for child in node.body
            ]
            body.append(
                ast.ClassDef(
                    name=node.name,
                    bases=node.bases,
                    keywords=node.keywords,
                    body=class_body,
                    decorator_list=node.decorator_list,
                    **({"type_params": getattr(node, "type_params", [])} if hasattr(node, "type_params") else {}),
                )
            )
        else:
            body.append(node)
    return ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)


def _function_shell(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return type(node)(
        name=node.name,
        args=node.args,
        body=[ast.Pass()],
        decorator_list=node.decorator_list,
        returns=node.returns,
        type_comment=node.type_comment,
        **({"type_params": getattr(node, "type_params", [])} if hasattr(node, "type_params") else {}),
    )


def _node_source(lines: list[str], node: ast.AST, target_indent: int) -> str:
    source = "".join(lines[node.lineno - 1 : node.end_lineno or node.lineno])
    source = textwrap.dedent(source)
    source = textwrap.indent(source, " " * target_indent)
    if source and not source.endswith(("\n", "\r")):
        source += "\n"
    return source


def _deduplicate_findings(findings: list[SafetyFinding]) -> list[SafetyFinding]:
    unique: list[SafetyFinding] = []
    seen: set[tuple[str, str, int | None]] = set()
    for finding in findings:
        key = (finding.rule, finding.message, finding.lineno)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _matches_blocked_suffix(call_name: str) -> bool:
    return call_name == "unlink" or call_name.endswith(".unlink") or call_name.endswith(".rmtree")


class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.complexity = 1

    def visit_If(self, node: ast.If) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.complexity += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.complexity += max(len(node.values) - 1, 0)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.complexity += len(node.handlers)
        if node.orelse:
            self.complexity += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.complexity += 1 + len(node.ifs)
        self.generic_visit(node)
