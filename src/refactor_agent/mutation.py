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


def generate_mutants(source: str, max_mutants: int = 8) -> list[Mutant]:
    tree = ast.parse(source)
    sites = _mutation_sites(tree)
    mutants: list[Mutant] = []
    for index, description in sites[:max_mutants]:
        mutated = _mutate_at(source, index)
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
) -> MutationTestResult:
    mutants = generate_mutants(candidate_source, max_mutants=max_mutants)
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


def _mutation_sites(tree: ast.AST) -> list[tuple[int, str]]:
    sites: list[tuple[int, str]] = []
    mutable_index = 0
    for node in ast.walk(tree):
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


def _mutate_at(source: str, target_index: int) -> str | None:
    tree = ast.parse(source)
    mutator = _SingleMutation(target_index)
    mutated = mutator.visit(tree)
    if not mutator.changed:
        return None
    ast.fix_missing_locations(mutated)
    try:
        return ast.unparse(mutated) + "\n"
    except RecursionError:
        return None


class _SingleMutation(ast.NodeTransformer):
    def __init__(self, target_index: int) -> None:
        self.target_index = target_index
        self.current_index = -1
        self.changed = False

    def visit(self, node: ast.AST):  # type: ignore[override]
        replacement = self._replacement(node)
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
