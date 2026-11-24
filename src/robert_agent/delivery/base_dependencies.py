"""Certificates for a closed, declarative Python module dependency graph.

Any executable/dynamic construct, external input or ambiguous module layout makes
this proof unavailable. Unknown dependency coverage always requires review.
"""

import ast
import hashlib
import math
from pathlib import PurePosixPath
import sys


class IncompleteDependencies(ValueError):
    pass


def documentation_path(name):
    item = PurePosixPath(name)
    return item.name != "AGENTS.md" and (
        name in {"README.md", "CHANGELOG.md"} or name.startswith("docs/") and item.suffix in {".md", ".rst"}
    )


def _literal(value):
    # Only immutable scalar constants: no evaluation hooks or shared mutable state.
    return (
        isinstance(value, ast.Constant)
        and type(value.value) in {str, int, float, bool, type(None)}
        and (type(value.value) is not float or math.isfinite(value.value))
    )


def closed_dependencies(sources):
    """Return complete, runtime-valid edges for flat declarative local modules."""
    modules = {name[:-3]: name for name in sources}
    if any(
        "/" in name
        or not name[:-3].isidentifier()
        or name[:-3].startswith("__")
        or name[:-3] in sys.stdlib_module_names
        for name in sources
    ):
        raise IncompleteDependencies("module resolution is outside the closed certificate")
    edges = {}
    hashes = {}
    imports = {}
    assignments = {}
    for name, text in sorted(sources.items()):
        tree = ast.parse(text)
        dependencies = set()
        names = set()
        imported = []
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                if not all(isinstance(target, ast.Name) for target in statement.targets) or not _literal(
                    statement.value
                ):
                    raise IncompleteDependencies("non-declarative assignment")
                names.update(target.id for target in statement.targets)
            elif isinstance(statement, ast.ImportFrom):
                if (
                    statement.level
                    or statement.module not in modules
                    or any(item.name == "*" for item in statement.names)
                ):
                    raise IncompleteDependencies("unknown import dependency")
                dependency = modules[statement.module]
                dependencies.add(dependency)
                imported.extend((dependency, item.name, item.asname or item.name) for item in statement.names)
            elif isinstance(statement, ast.Import):
                if any(item.name not in modules for item in statement.names):
                    raise IncompleteDependencies("unknown import dependency")
                dependencies.update(modules[item.name] for item in statement.names)
                names.update(item.asname or item.name for item in statement.names)
            elif (
                isinstance(statement, ast.Expr) and _literal(statement.value) and isinstance(statement.value.value, str)
            ):
                continue
            else:
                raise IncompleteDependencies("executable or dynamic Python input")
        if any(name.startswith("__") for name in names) or any(
            symbol.startswith("__") or alias.startswith("__") for _, symbol, alias in imported
        ):
            raise IncompleteDependencies("Python import metadata mutation")
        edges[name] = sorted(dependencies)
        hashes[name] = hashlib.sha256(text.encode()).hexdigest()
        assignments[name] = names
        imports[name] = imported
    exports = {}
    visiting = set()

    def resolve(name):
        if name in exports:
            return exports[name]
        if name in visiting:
            raise IncompleteDependencies("dependency cycle")
        visiting.add(name)
        for dependency in edges[name]:
            resolve(dependency)
        names = set(assignments[name])
        for dependency, symbol, alias in imports[name]:
            if symbol not in exports[dependency]:
                raise IncompleteDependencies("imported symbol does not exist")
            names.add(alias)
        visiting.remove(name)
        exports[name] = names
        return names

    for name in edges:
        resolve(name)
    return {"source_sha256": hashes, "dependencies": edges}


def dependency_certificate(revisions, upstream, reviewed):
    """Prove upstream is outside the candidate's dependency/caller component."""
    edges = {}
    evidence = {}
    for revision, sources in revisions.items():
        evidence[revision] = closed_dependencies(sources)
        for name, dependencies in evidence[revision]["dependencies"].items():
            edges.setdefault(name, set())
            for dependency in dependencies:
                edges[name].add(dependency)
                edges.setdefault(dependency, set()).add(name)
    # Callers and dependencies are both relevant; use undirected transitive closure.
    affected = set(reviewed)
    pending = list(affected)
    while pending:
        for name in edges.get(pending.pop(), ()):
            if name not in affected:
                affected.add(name)
                pending.append(name)
    if set(upstream) & affected:
        return None
    return {"method": "closed-declarative-python-v1", "revisions": evidence, "reviewed_component": sorted(affected)}
