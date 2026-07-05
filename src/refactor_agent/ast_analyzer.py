from __future__ import annotations

import ast
from collections.abc import Iterable

from refactor_agent.models import AstAnalysis, CandidateValidationResult, ClassSummary, FunctionSignature, SafetyFinding


HIGH_COMPLEXITY_THRESHOLD = 4
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

    original_functions = {item.qualified_name: item for item in original.functions}
    candidate_functions = {item.qualified_name: item for item in candidate.functions}
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
