from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from math import log2
import textwrap
import re

from refactor_agent.models import (
    AstAnalysis,
    AstRewriteResult,
    CandidateValidationResult,
    ClassSummary,
    FunctionSignature,
    SafetyFinding,
    TargetRegion,
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


def select_target_regions(
    source: str,
    issue_text: str = "",
    failure_feedback: str | None = None,
    max_regions: int = 3,
) -> list[TargetRegion]:
    analysis = analyze_ast(source)
    functions = [
        *analysis.functions,
        *(method for class_summary in analysis.classes for method in class_summary.methods),
    ]
    evidence = f"{issue_text}\n{failure_feedback or ''}".lower()
    evidence_tokens = set(re.findall(r"[a-z_][a-z0-9_]*", evidence))
    traceback_lines = {
        int(value)
        for value in re.findall(r"(?:\bline\s+|[\w./\\-]+\.py:)(\d+)\b", evidence)
    }
    tree = ast.parse(source)
    candidates: list[TargetRegion] = []
    for item in functions:
        score = item.complexity * 5
        reasons = [f"complexity={item.complexity}"]
        qualified = item.qualified_name.lower()
        name = item.name.lower()
        if _contains_symbol(evidence, qualified):
            score += 1000
            reasons.append("exact qualified symbol in evidence")
        elif name in evidence_tokens:
            score += 500
            reasons.append("symbol token in evidence")
        if any(item.lineno <= line <= (item.end_lineno or item.lineno) for line in traceback_lines):
            score += 800
            reasons.append("traceback line in region")
        metrics = _subtree_metrics(tree, item.qualified_name)
        candidates.append(
            TargetRegion(
                qualified_name=item.qualified_name,
                lineno=item.lineno,
                end_lineno=item.end_lineno or item.lineno,
                complexity=item.complexity,
                node_count=int(metrics["nodes"]),
                structural_entropy=metrics["entropy"],
                kind="method" if "." in item.qualified_name else "function",
                score=score,
                reason="; ".join(reasons),
            )
        )
    for node in tree.body:
        if not _is_module_target(node):
            continue
        score = 0
        reasons: list[str] = []
        if any(node.lineno <= line <= (node.end_lineno or node.lineno) for line in traceback_lines):
            score += 1200
            reasons.append("explicit module statement line in evidence")
        symbols = _module_statement_symbols(node)
        named_symbols = sorted(symbols & evidence_tokens)
        if named_symbols:
            score += 700
            reasons.append(f"module symbol in evidence: {', '.join(named_symbols)}")
        if not score:
            continue
        metrics = _node_metrics(node)
        candidates.append(
            TargetRegion(
                qualified_name=_module_target_key(node),
                lineno=node.lineno,
                end_lineno=node.end_lineno or node.lineno,
                complexity=metrics["complexity"],
                node_count=metrics["nodes"],
                structural_entropy=metrics["entropy"],
                kind="module",
                score=score,
                reason="; ".join(reasons),
            )
        )
    ranked = sorted(candidates, key=lambda item: (item.score, item.complexity, item.end_lineno), reverse=True)
    evidence_matches = [item for item in ranked if item.reason != f"complexity={item.complexity}"]
    return (evidence_matches or ranked[:1])[:max_regions]


def _contains_symbol(evidence: str, symbol: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(symbol)}(?![\w.])", evidence) is not None


def controlled_subtree_rewrite(
    original_source: str,
    candidate_source: str,
    allowed_regions: list[str | TargetRegion] | None = None,
    allowed_import_roots: set[str] | None = None,
) -> AstRewriteResult:
    try:
        original_tree = ast.parse(original_source)
    except SyntaxError as exc:
        finding = SafetyFinding(rule="original-syntax", message=str(exc), lineno=exc.lineno)
        return AstRewriteResult(ok=False, source=original_source, findings=[finding])
    selected = select_target_regions(original_source) if allowed_regions is None else allowed_regions
    selected_regions = [item for item in selected if isinstance(item, TargetRegion)]
    allowed = [item.qualified_name if isinstance(item, TargetRegion) else item for item in selected]
    try:
        candidate_tree = ast.parse(candidate_source)
    except SyntaxError as exc:
        finding = SafetyFinding(rule="candidate-syntax", message=str(exc), lineno=exc.lineno)
        return AstRewriteResult(
            ok=False,
            source=original_source,
            selected_regions=selected_regions,
            allowed_regions=allowed,
            findings=[finding],
        )

    allowed_functions = [name for name in allowed if not name.startswith("module:")]
    allowed_modules = [name for name in allowed if name.startswith("module:")]
    import_findings, added_import_nodes, added_import_texts = _import_change_findings(
        original_tree,
        candidate_tree,
        allowed_import_roots or set(),
    )
    findings: list[SafetyFinding] = []
    original_nodes = _qualified_function_nodes(original_tree)
    candidate_nodes = _qualified_function_nodes(candidate_tree)
    changed_functions = sorted(
        name
        for name in set(original_nodes) | set(candidate_nodes)
        if _node_fingerprint(original_nodes.get(name)) != _node_fingerprint(candidate_nodes.get(name))
    )

    module_targets, module_findings = _resolve_module_targets(original_tree, candidate_tree, allowed_modules)
    findings.extend(module_findings)
    ignored_module_slots = {slot for slot, _, _ in module_targets.values()}
    findings.extend(_module_boundary_findings(original_tree, candidate_tree, ignored_module_slots))
    findings.extend(import_findings)
    for name in changed_functions:
        if name not in allowed_functions:
            node = candidate_nodes.get(name) or original_nodes.get(name)
            findings.append(
                SafetyFinding(
                    rule="non-target-changed",
                    message=f"Function or method {name!r} changed outside the allowed AST regions {allowed!r}.",
                    lineno=getattr(node, "lineno", None),
                )
            )
    for name in allowed_functions:
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

    changed_modules = sorted(
        name
        for name, (_, original_node, candidate_node) in module_targets.items()
        if _node_fingerprint(original_node) != _node_fingerprint(candidate_node)
    )
    changed = sorted([*changed_functions, *changed_modules])
    validation = validate_candidate_source(original_source, candidate_source)
    findings.extend(validation.findings)
    findings = _deduplicate_findings(findings)
    if findings:
        return AstRewriteResult(
            ok=False,
            source=original_source,
            selected_regions=selected_regions,
            allowed_regions=allowed,
            changed_regions=changed,
            findings=findings,
        )

    original_lines = original_source.splitlines(keepends=True)
    candidate_lines = candidate_source.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []
    for name in changed:
        if name.startswith("module:"):
            _, original_node, candidate_node = module_targets[name]
        else:
            original_node = original_nodes[name]
            candidate_node = candidate_nodes[name]
        replacement = _node_source(candidate_lines, candidate_node, original_node.col_offset)
        replacements.append((original_node.lineno - 1, original_node.end_lineno or original_node.lineno, replacement))
    for start, end, replacement in sorted(replacements, reverse=True):
        original_lines[start:end] = [replacement]
    added_imports = [_node_source(candidate_lines, node, 0).rstrip() for node in added_import_nodes]
    if added_imports:
        insertion = _import_insertion_line(original_tree)
        original_lines[insertion:insertion] = [f"{value}\n" for value in added_imports]
    rewritten = "".join(original_lines)
    final_validation = validate_candidate_source(original_source, rewritten)
    if not final_validation.ok:
        return AstRewriteResult(
            ok=False,
            source=original_source,
            selected_regions=selected_regions,
            allowed_regions=allowed,
            changed_regions=changed,
            findings=final_validation.findings,
        )
    return AstRewriteResult(
        ok=True,
        source=rewritten,
        selected_regions=selected_regions,
        allowed_regions=allowed,
        changed_regions=changed,
        added_imports=added_import_texts,
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


def _module_boundary_findings(
    original: ast.Module,
    candidate: ast.Module,
    ignored_module_slots: set[int] | None = None,
) -> list[SafetyFinding]:
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
    ignored = ignored_module_slots or set()
    if _non_function_fingerprint(original, ignored) != _non_function_fingerprint(candidate, ignored):
        findings.append(
            SafetyFinding(
                rule="module-boundary-changed",
                message="Imports, classes, assignments, or other non-function module structure changed.",
            )
        )
    return findings


def _import_change_findings(
    original: ast.Module,
    candidate: ast.Module,
    allowed_roots: set[str],
) -> tuple[list[SafetyFinding], list[ast.Import | ast.ImportFrom], list[str]]:
    original_entries = _import_entries(original)
    candidate_entries = _import_entries(candidate)
    original_imports = Counter((scope, _node_fingerprint(node)) for scope, node in original_entries)
    candidate_imports = Counter((scope, _node_fingerprint(node)) for scope, node in candidate_entries)
    findings: list[SafetyFinding] = []
    if any(count > candidate_imports[key] for key, count in original_imports.items()):
        findings.append(SafetyFinding(rule="import-removed-or-changed", message="Existing imports cannot be removed or rewritten."))
    additions: list[tuple[str, ast.Import | ast.ImportFrom]] = []
    remaining = original_imports.copy()
    for scope, node in candidate_entries:
        key = (scope, _node_fingerprint(node))
        if remaining[key]:
            remaining[key] -= 1
        else:
            additions.append((scope, node))
    normalized_allowed = {root.split(".")[0] for root in allowed_roots}
    accepted_module: list[ast.Import | ast.ImportFrom] = []
    accepted_text: list[str] = []
    for scope, node in additions:
        roots = _import_roots(node)
        if isinstance(node, ast.ImportFrom) and node.level:
            findings.append(SafetyFinding(rule="relative-import-added", message="New relative imports are not allowed.", lineno=node.lineno))
            continue
        if any(alias.name == "*" for alias in node.names):
            findings.append(SafetyFinding(rule="wildcard-import-added", message="New wildcard imports are not allowed.", lineno=node.lineno))
            continue
        denied = sorted(root for root in roots if root not in normalized_allowed or root in BLOCKED_IMPORTS)
        if denied:
            findings.append(
                SafetyFinding(
                    rule="import-not-allowlisted",
                    message=f"New import roots are not allowlisted: {', '.join(denied)}.",
                    lineno=node.lineno,
                )
            )
            continue
        if scope == "module":
            accepted_module.append(node)
        accepted_text.append(ast.unparse(node))
    return findings, accepted_module, accepted_text


def _import_entries(tree: ast.Module) -> list[tuple[str, ast.Import | ast.ImportFrom]]:
    visitor = _ImportScopeVisitor()
    visitor.visit(tree)
    return visitor.entries


class _ImportScopeVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.entries: list[tuple[str, ast.Import | ast.ImportFrom]] = []

    def visit_Import(self, node: ast.Import) -> None:
        self.entries.append((".".join(self.scope) or "module", node))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.entries.append((".".join(self.scope) or "module", node))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _import_roots(node: ast.Import | ast.ImportFrom) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    return {(node.module or "").split(".")[0]}


def _import_insertion_line(tree: ast.Module) -> int:
    insertion = 0
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            insertion = node.end_lineno or node.lineno
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insertion = node.end_lineno or node.lineno
            continue
        break
    return insertion


def _non_function_fingerprint(tree: ast.Module, ignored_module_slots: set[int] | None = None) -> str:
    body: list[ast.stmt] = []
    normalized_index = 0
    ignored = ignored_module_slots or set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if normalized_index in ignored:
            body.append(ast.Pass())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
        normalized_index += 1
    return ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)


def _resolve_module_targets(
    original: ast.Module,
    candidate: ast.Module,
    allowed: list[str],
) -> tuple[dict[str, tuple[int, ast.stmt, ast.stmt]], list[SafetyFinding]]:
    original_body = [node for node in original.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    candidate_body = [node for node in candidate.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    resolved: dict[str, tuple[int, ast.stmt, ast.stmt]] = {}
    findings: list[SafetyFinding] = []
    for target in allowed:
        original_match = next(
            (
                (index, node)
                for index, node in enumerate(original_body)
                if _module_target_key(node) == target and _is_module_target(node)
            ),
            None,
        )
        if original_match is None:
            findings.append(
                SafetyFinding(
                    rule="target-region-missing",
                    message=f"Allowed AST region {target!r} does not identify an eligible top-level statement.",
                )
            )
            continue
        index, original_node = original_match
        if index >= len(candidate_body):
            findings.append(
                SafetyFinding(
                    rule="target-region-missing",
                    message=f"Candidate removed allowed AST region {target!r}.",
                    lineno=original_node.lineno,
                )
            )
            continue
        candidate_node = candidate_body[index]
        if type(candidate_node) is not type(original_node):
            findings.append(
                SafetyFinding(
                    rule="target-region-kind-changed",
                    message=f"Allowed AST region {target!r} changed statement type.",
                    lineno=candidate_node.lineno,
                )
            )
            continue
        if _module_binding_fingerprint(candidate_node) != _module_binding_fingerprint(original_node):
            findings.append(
                SafetyFinding(
                    rule="module-target-binding-changed",
                    message=f"Allowed AST region {target!r} changed its module-level binding.",
                    lineno=candidate_node.lineno,
                )
            )
            continue
        resolved[target] = (index, original_node, candidate_node)
    return resolved, findings


def _is_module_target(node: ast.stmt) -> bool:
    return isinstance(
        node,
        (
            ast.Assign,
            ast.AnnAssign,
            ast.AugAssign,
            ast.If,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.Try,
            ast.With,
            ast.AsyncWith,
            ast.Match,
        ),
    )


def _module_target_key(node: ast.stmt) -> str:
    return f"module:{node.lineno}:{type(node).__name__}"


def _module_binding_fingerprint(node: ast.stmt) -> str | None:
    if isinstance(node, ast.Assign):
        return ast.dump(ast.Tuple(elts=node.targets, ctx=ast.Store()), include_attributes=False)
    if isinstance(node, ast.AnnAssign):
        return ast.dump(ast.Tuple(elts=[node.target, node.annotation], ctx=ast.Store()), include_attributes=False)
    if isinstance(node, ast.AugAssign):
        return ast.dump(node.target, include_attributes=False)
    return None


def _module_statement_symbols(node: ast.stmt) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            names.add(item.id.lower())
    return names


def _node_metrics(node: ast.AST) -> dict[str, int | float]:
    visitor = _ComplexityVisitor()
    visitor.visit(node)
    node_types = [type(item).__name__ for item in ast.walk(node)]
    counts = Counter(node_types)
    total = len(node_types)
    entropy = -sum((count / total) * log2(count / total) for count in counts.values()) if total else 0.0
    return {"complexity": visitor.complexity, "nodes": total, "entropy": entropy}


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
