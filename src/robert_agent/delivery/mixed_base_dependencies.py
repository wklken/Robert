"""Closed source and build graphs for independently packaged mixed components.

This is a complete declarative proof, not a directory exemption. Installation,
external modules, executable configuration and unknown CI commands are refused.
"""

import ast
import hashlib
import json
from pathlib import PurePosixPath
import re
import shlex
import tomllib

import yaml

from robert_agent.delivery.base_dependencies import IncompleteDependencies, closed_dependencies, documentation_path


def _load_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise IncompleteDependencies("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique)


def _python(sources):
    modules = {}
    for name in sources:
        path = PurePosixPath(name)
        parts = list(path.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if not parts or any(not part.isidentifier() or part.startswith("__") for part in parts):
            raise IncompleteDependencies("ambiguous nested module")
        module = ".".join(parts)
        if module in modules:
            raise IncompleteDependencies("module/package collision")
        modules[module] = name
    aliases = {module: f"module_{i}" for i, module in enumerate(sorted(modules))}
    transformed = {}
    package_edges = {}
    for module, name in modules.items():
        path = PurePosixPath(name)
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        parents = []
        current = package
        while current:
            if current in modules and modules[current] != name:
                parents.append(modules[current])
            current = current.rpartition(".")[0]
        package_edges[name] = parents
        tree = ast.parse(sources[name])
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for item in statement.names:
                    if item.name not in modules:
                        raise IncompleteDependencies("external nested import")
                    item.asname = item.asname or item.name.split(".")[0]
                    item.name = aliases[item.name]
            elif isinstance(statement, ast.ImportFrom):
                target = statement.module or ""
                if statement.level:
                    parts = package.split(".") if package else []
                    if statement.level > len(parts):
                        raise IncompleteDependencies("relative import outside package")
                    target = ".".join(parts[: len(parts) - statement.level + 1] + ([target] if target else []))
                if target not in modules:
                    raise IncompleteDependencies("unknown nested import")
                if any(f"{target}.{item.name}" in modules for item in statement.names):
                    raise IncompleteDependencies("ambiguous package submodule import")
                statement.module = aliases[target]
                statement.level = 0
        transformed[aliases[module] + ".py"] = ast.unparse(tree)
    evidence = closed_dependencies(transformed)
    names = {aliases[module] + ".py": name for module, name in modules.items()}
    edges = {
        names[name]: sorted(set(package_edges[names[name]]) | {names[dep] for dep in dependencies})
        for name, dependencies in evidence["dependencies"].items()
    }
    # Importing a nested module executes its package initializers. Reject cycles
    # introduced by those edges rather than assume initializer order is harmless.
    remaining = set(edges)
    while remaining:
        ready = {name for name in remaining if not (set(edges[name]) & remaining)}
        if not ready:
            raise IncompleteDependencies("package initializer cycle")
        remaining.difference_update(ready)
    return edges


def _javascript(sources):
    edges, exports, imported = {}, {}, {}
    scalar = r'(?:"[^"\\\r\n]*"|\'[^\'\\\r\n]*\'|-?(?:0|[1-9]\d*)(?:\.\d+)?|true|false|null)'
    reserved = set(
        "await break case catch class const continue debugger default delete do else enum export extends false "
        "finally for function if implements import in instanceof interface let new null package private protected "
        "public return static super switch this throw true try typeof var void while with yield".split()
    )
    constant = re.compile(rf"(export\s+)?const\s+([A-Za-z_]\w*)\s*=\s*({scalar})\s*;")
    import_statement = re.compile(r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]\s*;")
    for name, text in sources.items():
        dependencies, symbols, imports, declared = set(), set(), [], set()
        while text.strip():
            text = text.lstrip()
            if text.startswith("//"):
                text = text.partition("\n")[2]
                continue
            if text.startswith("/*"):
                _, separator, text = text.partition("*/")
                if not separator:
                    raise IncompleteDependencies("unterminated JavaScript comment")
                continue
            match = constant.match(text)
            if match:
                if match[2] in declared or match[2] in reserved:
                    raise IncompleteDependencies("duplicate JavaScript binding")
                declared.add(match[2])
                if match[1]:
                    symbols.add(match[2])
                text = text[match.end() :]
                continue
            match = import_statement.match(text)
            if not match or not match[2].startswith("./"):
                raise IncompleteDependencies("executable or external JavaScript input")
            relative = PurePosixPath(match[2])
            if ".." in relative.parts or relative.suffix not in {".js", ".ts"}:
                raise IncompleteDependencies("ambiguous JavaScript resolution")
            dependency = str(PurePosixPath(name).parent / relative)
            if dependency not in sources:
                raise IncompleteDependencies("missing JavaScript dependency")
            for symbol in match[1].split(","):
                binding = re.fullmatch(r"\s*([A-Za-z_]\w*)(?:\s+as\s+([A-Za-z_]\w*))?\s*", symbol)
                if not binding:
                    raise IncompleteDependencies("unknown JavaScript binding")
                alias = binding[2] or binding[1]
                if alias in declared or alias in reserved or binding[1] in reserved:
                    raise IncompleteDependencies("duplicate JavaScript binding")
                declared.add(alias)
                imports.append((dependency, binding[1]))
            dependencies.add(dependency)
            text = text[match.end() :]
        edges[name], exports[name], imported[name] = sorted(dependencies), symbols, imports
    for bindings in imported.values():
        if any(symbol not in exports[dependency] for dependency, symbol in bindings):
            raise IncompleteDependencies("missing JavaScript export")
    return edges


def _command(command, directory, sources):
    """Only syntax compilation: no imports, installs, shell or lifecycle hooks."""
    args = shlex.split(command)
    if args[:3] == ["python", "-m", "compileall"] and len(args) == 4:
        target = args[3]
    elif args[:2] == ["node", "--check"] and len(args) == 3:
        target = args[2]
    else:
        raise IncompleteDependencies("unknown build/CI command")
    path = PurePosixPath(target)
    if path.is_absolute() or ".." in path.parts or any(char in target for char in "$;|&*`\\"):
        raise IncompleteDependencies("build path leaves component")
    full = str(PurePosixPath(directory) / path)
    paths = {name for name in sources if name == full or name.startswith(full + "/")}
    if not paths:
        raise IncompleteDependencies("missing build input")
    return paths


def _package(name, text, sources):
    directory = str(PurePosixPath(name).parent)
    members = {item for item in sources if item.startswith(directory + "/")}
    if name.endswith("pyproject.toml"):
        data = tomllib.loads(text)
        project = data.get("project", {})
        if (
            set(data) != {"project"}
            or set(project) - {"name", "version", "description", "requires-python", "dependencies"}
            or project.get("dependencies", []) != []
            or not isinstance(project.get("name"), str)
        ):
            raise IncompleteDependencies("external or executable Python packaging")
        if not any(item.endswith(".py") for item in members):
            raise IncompleteDependencies("Python package has no source")
    else:
        data = _load_json(text)
        if (
            not isinstance(data, dict)
            or set(data) - {"name", "private", "version", "type", "scripts", "dependencies", "devDependencies"}
            or data.get("dependencies", {}) != {}
            or data.get("devDependencies", {}) != {}
            or data.get("type") != "module"
            or data.get("private") is not True
            or not isinstance(data.get("name"), str)
            or not isinstance(data.get("scripts", {}), dict)
        ):
            raise IncompleteDependencies("external or executable Node packaging")
        for script, command in data.get("scripts", {}).items():
            if script not in {"check", "build", "test"} or not isinstance(command, str):
                raise IncompleteDependencies("Node lifecycle hook")
            _command(command, directory, sources)
    return members


class _WorkflowLoader(yaml.SafeLoader):
    pass


def _unique_yaml(loader, node):
    result = {}
    for key, value in node.value:
        key = loader.construct_object(key, deep=True)
        if key in result:
            raise IncompleteDependencies("duplicate workflow key")
        result[key] = loader.construct_object(value, deep=True)
    return result


_WorkflowLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_yaml)


def _workflow(text, sources, packages):
    if re.search(r"[&*]|\$\{\{|!", text.replace("/**", "")):
        raise IncompleteDependencies("dynamic workflow")
    try:
        data = yaml.load(text, Loader=_WorkflowLoader)
    except (yaml.YAMLError, TypeError) as exc:
        raise IncompleteDependencies("malformed workflow") from exc
    if not isinstance(data, dict) or set(data) - {"name", "on", True, "jobs"}:
        raise IncompleteDependencies("unknown workflow metadata")
    if "on" in data and True in data:
        raise IncompleteDependencies("ambiguous workflow trigger")
    triggers = data.get("on", data.get(True))
    if not isinstance(triggers, dict) or not triggers or set(triggers) - {"push", "pull_request"}:
        raise IncompleteDependencies("unknown workflow trigger")
    directories = set()
    for trigger in triggers.values():
        if not isinstance(trigger, dict) or set(trigger) - {"paths", "branches"}:
            raise IncompleteDependencies("unknown workflow filters")
        paths = trigger.get("paths")
        if (
            not isinstance(paths, list)
            or len(paths) != 1
            or not isinstance(paths[0], str)
            or not paths[0].endswith("/**")
        ):
            raise IncompleteDependencies("global workflow filter")
        directory = paths[0][:-3]
        if directory not in packages:
            raise IncompleteDependencies("workflow component is not packaged")
        directories.add(directory)
    if len(directories) != 1:
        raise IncompleteDependencies("cross component workflow")
    directory = directories.pop()
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise IncompleteDependencies("missing workflow jobs")
    for job in jobs.values():
        if not isinstance(job, dict) or set(job) - {"runs-on", "steps"} or job.get("runs-on") != "ubuntu-latest":
            raise IncompleteDependencies("dynamic or external workflow job")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            raise IncompleteDependencies("missing workflow steps")
        for step in steps:
            if not isinstance(step, dict):
                raise IncompleteDependencies("unknown workflow step")
            if set(step) <= {"uses", "name"} and step.get("uses") == "actions/checkout@v4":
                continue
            if (
                set(step) - {"name", "run", "working-directory"}
                or step.get("working-directory") != directory
                or not isinstance(step.get("run"), str)
            ):
                raise IncompleteDependencies("external or cross component workflow step")
            _command(step["run"], directory, sources)
    return {name for name in sources if name.startswith(directory + "/")}


def mixed_dependencies(sources):
    """Hash every input and connect sources, package initialization and CI gates."""
    python = {name: text for name, text in sources.items() if name.endswith(".py")}
    javascript = {name: text for name, text in sources.items() if PurePosixPath(name).suffix in {".js", ".ts"}}
    edges = {**_python(python), **_javascript(javascript)}
    packages = {}
    for name, text in sources.items():
        if PurePosixPath(name).name in {"pyproject.toml", "package.json"}:
            members = _package(name, text, sources)
            packages[str(PurePosixPath(name).parent)] = name
            edges[name] = sorted(members - {name})
    if not python or not javascript or len(packages) < 2:
        raise IncompleteDependencies("mixed components are not independently packaged")
    for name, text in sources.items():
        if name in edges:
            continue
        if name.startswith(".github/workflows/") and PurePosixPath(name).suffix in {".yml", ".yaml"}:
            edges[name] = sorted(_workflow(text, sources, packages))
        elif PurePosixPath(name).name == "Makefile":
            directory = str(PurePosixPath(name).parent)
            if directory not in packages:
                raise IncompleteDependencies("global build")
            dependencies = set()
            for line in text.splitlines():
                if not line.strip() or line.startswith("#") or re.fullmatch(r"[A-Za-z_]\w*:\s*", line):
                    continue
                if not line.startswith("\t"):
                    raise IncompleteDependencies("dynamic Makefile")
                dependencies.update(_command(line.strip(), directory, sources))
            edges[name] = sorted(dependencies | {packages[directory]})
        elif documentation_path(name):
            edges[name] = []
        else:
            raise IncompleteDependencies("unknown source, acceptance, schema or configuration input")
    return {
        "source_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sorted(sources.items())},
        "dependencies": edges,
    }


def mixed_certificate(revisions, upstream, reviewed):
    evidence, edges = {}, {}
    for revision, sources in revisions.items():
        evidence[revision] = mixed_dependencies(sources)
        for name, dependencies in evidence[revision]["dependencies"].items():
            edges.setdefault(name, set())
            for dependency in dependencies:
                edges[name].add(dependency)
                edges.setdefault(dependency, set()).add(name)
    # Any descriptor/rule change needs fresh validation even outside the reviewed
    # component; static analysis must not silently bless changed build semantics.
    if any(PurePosixPath(name).suffix not in {".py", ".js", ".ts"} for name in upstream | reviewed):
        return None
    affected, pending = set(reviewed), list(reviewed)
    while pending:
        for name in edges.get(pending.pop(), ()):
            if name not in affected:
                affected.add(name)
                pending.append(name)
    if upstream & affected:
        return None
    return {"method": "closed-mixed-source-build-v1", "revisions": evidence, "reviewed_component": sorted(affected)}
