from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from refactor_agent.execution_control import ExecutionControl
from refactor_agent.models import MutationTestResult
from refactor_agent.sandbox import run_pytest_with_backend, write_candidate


@dataclass(frozen=True)
class Mutant:
    description: str
    source: str


def generate_mutants(
    source: str,
    max_mutants: int = 8,
    target_regions: list[str] | None = None,
) -> list[Mutant]:
    tree = ast.parse(source)
    sites = _mutation_sites(tree, target_regions)
    mutants: list[Mutant] = []
    for index, description in sites[:max_mutants]:
        mutated = _mutate_at(source, index, target_regions)
        if mutated and mutated != source:
            mutants.append(Mutant(description=description, source=mutated))
    return mutants


def run_mutation_tests(
    candidate_source: str,
    target_file: Path,
    workspace: Path,
    tests_path: Path,
    timeout_seconds: float = 30.0,
    max_mutants: int = 8,
    backend: str = "subprocess",
    docker_image: str = "refactor-agent-sandbox:py312",
    memory: str = "256m",
    cpus: float = 1.0,
    execution_control: ExecutionControl | None = None,
    target_regions: list[str] | None = None,
) -> MutationTestResult:
    mutants = generate_mutants(
        candidate_source,
        max_mutants=max_mutants,
        target_regions=target_regions,
    )
    killed = 0
    survived: list[str] = []
    for mutant in mutants:
        write_candidate(target_file, mutant.source)
        result = run_pytest_with_backend(
            workspace=workspace,
            tests_path=tests_path,
            timeout_seconds=timeout_seconds,
            backend=backend,
            docker_image=docker_image,
            memory=memory,
            cpus=cpus,
            execution_control=execution_control,
        )
        if result.passed:
            survived.append(mutant.description)
        else:
            killed += 1
    write_candidate(target_file, candidate_source)
    return MutationTestResult(
        total=len(mutants),
        killed=killed,
        survived=len(survived),
        survival_details=survived,
    )


def _mutation_sites(
    tree: ast.Module,
    target_regions: list[str] | None = None,
) -> list[tuple[int, str]]:
    sites: list[tuple[int, str]] = []
    mutable_index = 0
    allowed_node_ids = _mutation_node_ids(tree, target_regions)
    for node in ast.walk(tree):
        if allowed_node_ids is not None and id(node) not in allowed_node_ids:
            continue
        if isinstance(node, ast.Compare) and node.ops:
            sites.append((mutable_index, f"flip comparison at line {getattr(node, 'lineno', '?')}"))
            mutable_index += 1
        elif isinstance(node, ast.BoolOp):
            sites.append((mutable_index, f"flip boolean operator at line {getattr(node, 'lineno', '?')}"))
            mutable_index += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            sites.append((mutable_index, f"flip boolean literal at line {getattr(node, 'lineno', '?')}"))
            mutable_index += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            sites.append((mutable_index, f"nudge integer literal at line {getattr(node, 'lineno', '?')}"))
            mutable_index += 1
    return sites


def _mutate_at(
    source: str,
    target_index: int,
    target_regions: list[str] | None = None,
) -> str | None:
    tree = ast.parse(source)
    mutator = _SingleMutation(
        target_index,
        allowed_node_ids=_mutation_node_ids(tree, target_regions),
    )
    mutated = mutator.visit(tree)
    if not mutator.changed:
        return None
    ast.fix_missing_locations(mutated)
    try:
        return ast.unparse(mutated) + "\n"
    except RecursionError:
        return None


class _SingleMutation(ast.NodeTransformer):
    def __init__(self, target_index: int, allowed_node_ids: set[int] | None = None) -> None:
        self.target_index = target_index
        self.allowed_node_ids = allowed_node_ids
        self.current_index = -1
        self.changed = False

    def visit(self, node: ast.AST):  # type: ignore[override]
        replacement = (
            self._replacement(node)
            if self.allowed_node_ids is None or id(node) in self.allowed_node_ids
            else None
        )
        if replacement is not None:
            self.current_index += 1
        if replacement is not None and self.current_index == self.target_index and not self.changed:
            self.changed = True
            return ast.copy_location(replacement, node)
        return super().visit(node)

    def _replacement(self, node: ast.AST) -> ast.AST | None:
        if isinstance(node, ast.Compare) and node.ops:
            mutated = ast.Compare(left=node.left, ops=[_flip_cmp(node.ops[0]), *node.ops[1:]], comparators=node.comparators)
            return mutated
        if isinstance(node, ast.BoolOp):
            op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            return ast.BoolOp(op=op, values=node.values)
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return ast.Constant(value=not node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return ast.Constant(value=node.value + 1)
        return None


def _mutation_node_ids(
    tree: ast.Module,
    target_regions: list[str] | None,
) -> set[int] | None:
    if target_regions is None:
        return None
    function_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    module_nodes: dict[str, ast.stmt] = {}
    for node in tree.body:
        module_nodes[f"module:{node.lineno}:{type(node).__name__}"] = node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_nodes[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function_nodes[f"{node.name}.{child.name}"] = child
    resolved = {name: function_nodes.get(name) or module_nodes.get(name) for name in target_regions}
    missing = [name for name, node in resolved.items() if node is None]
    if missing:
        raise ValueError(f"Mutation target regions do not exist in candidate source: {missing!r}")
    roots = [node for node in resolved.values() if node is not None]
    return {id(node) for root in roots for node in ast.walk(root)}


def _flip_cmp(op: ast.cmpop) -> ast.cmpop:
    mapping: dict[type[ast.cmpop], ast.cmpop] = {
        ast.Eq: ast.NotEq(),
        ast.NotEq: ast.Eq(),
        ast.Lt: ast.GtE(),
        ast.LtE: ast.Gt(),
        ast.Gt: ast.LtE(),
        ast.GtE: ast.Lt(),
        ast.Is: ast.IsNot(),
        ast.IsNot: ast.Is(),
        ast.In: ast.NotIn(),
        ast.NotIn: ast.In(),
    }
    return mapping.get(type(op), ast.NotEq())
