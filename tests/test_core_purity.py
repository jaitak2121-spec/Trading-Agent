"""Makes the kernel's purity claim true rather than aspirational.

Three docstrings in this repository promise something about imports:

* :mod:`trading.core` -- stdlib, ``trading.core``, and :mod:`trading.ports` only.
* :mod:`trading.ports` -- stdlib and :mod:`trading.core` only; no infrastructure.
* :mod:`trading.adapters` -- no network I/O anywhere, and nothing in the kernel
  may import back into it.

A comment claiming that is worth nothing. This module reads every source file in
the package with :mod:`ast`, resolves every import to an absolute dotted name,
and checks it against the rule for the layer the file lives in. It then does the
same check at runtime in a fresh interpreter, because static analysis cannot see
a lazy import inside a function body that ``ast`` walks past without judging.

Two properties make this a real test rather than a decorative one:

* It fails loudly if it finds nothing to check. A purity test that silently
  stops discovering files is worse than no purity test, so
  :class:`TestTheCheckerItself` asserts the file census and exercises the
  relative-import resolver against known-tricky cases.
* The banned list is checked against the *whole* package, not just the kernel,
  so a network client cannot be smuggled in under ``adapters`` either -- which is
  the Stage 1 constraint, not merely a kernel-hygiene rule.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import unittest

#: Repository root, derived from this file so the tests do not depend on cwd.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "trading"

#: Layers. A module's rule is chosen by the longest matching prefix.
CORE = "trading.core"
PORTS = "trading.ports"
ADAPTERS = "trading.adapters"
STRATEGY = "trading.strategy"

#: What each layer may import from inside the package.
KERNEL_LAYERS = (CORE, PORTS)
LAYER_RULES: dict[str, frozenset[str]] = {
    # The kernel calls outward through ports, so core may see them.
    CORE: frozenset({CORE, PORTS}),
    # Ports are interfaces over core value types. They must not see core's
    # machinery's implementers, and above all not the adapters that implement them.
    PORTS: frozenset({CORE, PORTS}),
    # Adapters implement ports using core value types. They may not reach into
    # the strategy layer -- an adapter driving a strategy inverts the arrow.
    ADAPTERS: frozenset({CORE, PORTS, ADAPTERS}),
    # Strategies see core values and the port types they are handed. They may
    # not import a concrete adapter, which is how INVARIANT 3 stays structural.
    STRATEGY: frozenset({CORE, PORTS, STRATEGY}),
}

#: Modules that would put real infrastructure inside a Stage 1 file. Some are
#: stdlib and therefore invisible to a third-party check, which is exactly why
#: they are listed separately: ``import socket`` is stdlib and still forbidden.
BANNED_MODULES: frozenset[str] = frozenset(
    {
        # network
        "socket",
        "socketserver",
        "ssl",
        "select",
        "selectors",
        "asyncio",
        "http",
        "urllib",
        "ftplib",
        "imaplib",
        "poplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "wsgiref",
        "webbrowser",
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "websockets",
        # process control and native escape hatches
        "subprocess",
        "multiprocessing",
        "ctypes",
        "signal",
        # databases and frameworks, explicitly out of scope for Stage 1
        "sqlite3",
        "psycopg",
        "psycopg2",
        "sqlalchemy",
        "fastapi",
        "starlette",
        "uvicorn",
        "pydantic",
        "flask",
        "django",
        # numeric stacks that would smuggle floats into money maths
        "numpy",
        "pandas",
        "scipy",
    }
)

#: Dynamic import machinery. Banned in the kernel because it would let a module
#: reach a layer the static check cannot see.
DYNAMIC_IMPORT_MODULES: frozenset[str] = frozenset({"importlib", "imp", "pkgutil"})
DYNAMIC_CALLS: frozenset[str] = frozenset({"__import__", "eval", "exec", "compile"})

#: Every source file in the package, discovered once.
SOURCE_FILES: tuple[pathlib.Path, ...] = tuple(sorted(PACKAGE_ROOT.rglob("*.py")))


def module_name_of(path: pathlib.Path) -> str:
    """Dotted module name for a file inside the package."""
    relative = path.relative_to(REPO_ROOT)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def resolve_relative(module_name: str, is_package: bool, level: int, module: str) -> str:
    """Turn a ``from ..x import y`` into an absolute dotted name.

    ``level`` 1 means the current package; each additional level walks one
    package upward. For a non-package module the "current package" is the
    package containing it, which is why ``is_package`` matters.
    """
    parts = module_name.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        drop = level - 1
        if drop >= len(parts):
            raise ValueError(f"relative import escapes the package in {module_name}")
        parts = parts[:-drop]
    if module:
        parts = parts + module.split(".")
    return ".".join(parts)


def imports_of(path: pathlib.Path) -> list[str]:
    """Every module ``path`` imports, as absolute dotted names.

    ``from . import x`` yields the package itself, which is harmless: the layer
    check only looks at prefixes.
    """
    module_name = module_name_of(path)
    is_package = path.name == "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append(
                    resolve_relative(
                        module_name, is_package, node.level, node.module or ""
                    )
                )
            elif node.module:
                found.append(node.module)
    return found


def layer_of(module_name: str) -> str | None:
    """Which layer rule applies, or ``None`` for ``trading`` itself."""
    best: str | None = None
    for layer in LAYER_RULES:
        if module_name == layer or module_name.startswith(layer + "."):
            if best is None or len(layer) > len(best):
                best = layer
    return best


def top_level(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def files_in(layer: str) -> list[pathlib.Path]:
    return [path for path in SOURCE_FILES if layer_of(module_name_of(path)) == layer]


class PurityCase(unittest.TestCase):
    """Shared assertions phrased so a failure names the file and the import."""

    def assertImportAllowed(
        self, path: pathlib.Path, imported: str, allowed: frozenset[str]
    ) -> None:
        if top_level(imported) != "trading":
            self.assertIn(
                top_level(imported),
                sys.stdlib_module_names,
                f"{path.relative_to(REPO_ROOT)} imports third-party '{imported}'; "
                "Stage 1 is standard library only",
            )
            return
        layer = layer_of(imported)
        if layer is None:
            # ``trading`` itself holds only a version string. Importing it is fine.
            self.assertEqual(imported, "trading", f"unclassifiable import {imported}")
            return
        self.assertIn(
            layer,
            allowed,
            f"{path.relative_to(REPO_ROOT)} imports '{imported}' from layer "
            f"'{layer}', which is not one of {sorted(allowed)}",
        )


class TestTheCheckerItself(PurityCase):
    """Guards against a vacuous pass.

    Every other class here reports success by finding no violations. That is
    indistinguishable from finding no files, so the census is asserted first.
    """

    def test_the_package_is_where_we_think_it_is(self) -> None:
        self.assertTrue(PACKAGE_ROOT.is_dir(), f"no package at {PACKAGE_ROOT}")

    def test_enough_files_were_discovered(self) -> None:
        # 17 core modules plus package inits, ports, adapters, strategy. The
        # exact number will grow; the point is that discovery is not empty or
        # nearly so, which would make every check below trivially pass.
        self.assertGreaterEqual(len(SOURCE_FILES), 20, "discovery found too little")

    def test_every_layer_actually_has_files(self) -> None:
        for layer in LAYER_RULES:
            with self.subTest(layer=layer):
                self.assertTrue(files_in(layer), f"no files found for layer {layer}")

    def test_the_kernel_is_the_bulk_of_the_package(self) -> None:
        kernel = sum(len(files_in(layer)) for layer in KERNEL_LAYERS)
        self.assertGreaterEqual(kernel, 15, "kernel file discovery looks wrong")

    def test_resolver_handles_a_sibling_import(self) -> None:
        # trading/core/gateway.py: from .audit import ...
        self.assertEqual(
            resolve_relative("trading.core.gateway", False, 1, "audit"),
            "trading.core.audit",
        )

    def test_resolver_handles_a_parent_import(self) -> None:
        # trading/core/gateway.py: from ..ports.broker import ...
        self.assertEqual(
            resolve_relative("trading.core.gateway", False, 2, "ports.broker"),
            "trading.ports.broker",
        )

    def test_resolver_handles_a_grandparent_import_from_a_nested_package(self) -> None:
        # trading/adapters/memory/broker.py: from ...core.authz import ...
        self.assertEqual(
            resolve_relative("trading.adapters.memory.broker", False, 3, "core.authz"),
            "trading.core.authz",
        )

    def test_resolver_handles_a_package_init(self) -> None:
        # trading/ports/__init__.py: from .broker import ...
        self.assertEqual(
            resolve_relative("trading.ports", True, 1, "broker"),
            "trading.ports.broker",
        )

    def test_resolver_handles_a_bare_relative_import(self) -> None:
        self.assertEqual(
            resolve_relative("trading.core.gateway", False, 1, ""), "trading.core"
        )

    def test_resolver_refuses_to_escape_the_package(self) -> None:
        with self.assertRaises(ValueError):
            resolve_relative("trading.core.gateway", False, 9, "elsewhere")

    def test_layer_of_prefers_the_longest_match(self) -> None:
        self.assertEqual(layer_of("trading.core.gateway"), CORE)
        self.assertEqual(layer_of("trading.adapters.memory.broker"), ADAPTERS)
        self.assertIsNone(layer_of("trading"))

    def test_layer_of_does_not_match_a_partial_name(self) -> None:
        # 'trading.corekit' must not be read as living in 'trading.core'.
        self.assertIsNone(layer_of("trading.corekit"))

    def test_every_source_file_parses(self) -> None:
        for path in SOURCE_FILES:
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class TestCoreIsPure(PurityCase):
    """INVARIANT: the safety core depends on nothing but stdlib and the kernel."""

    def test_core_imports_only_stdlib_core_and_ports(self) -> None:
        for path in files_in(CORE):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertImportAllowed(path, imported, LAYER_RULES[CORE])

    def test_core_never_imports_an_adapter(self) -> None:
        for path in files_in(CORE):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertFalse(
                        imported.startswith(ADAPTERS),
                        f"{path.name} imports adapter '{imported}'; the safety "
                        "core must not depend on infrastructure",
                    )

    def test_core_never_imports_a_strategy(self) -> None:
        for path in files_in(CORE):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertFalse(
                        imported.startswith(STRATEGY),
                        f"{path.name} imports '{imported}'; the core must not "
                        "know about strategies",
                    )

    def test_core_never_imports_the_tests(self) -> None:
        for path in files_in(CORE):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertNotEqual(top_level(imported), "tests")


class TestPortsAreInterfacesOnly(PurityCase):
    """A port that imports its own implementation is not a boundary."""

    def test_ports_import_only_stdlib_and_core(self) -> None:
        for path in files_in(PORTS):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertImportAllowed(path, imported, LAYER_RULES[PORTS])

    def test_ports_never_import_an_adapter(self) -> None:
        for path in files_in(PORTS):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertFalse(imported.startswith(ADAPTERS))

    def test_ports_never_import_a_strategy(self) -> None:
        for path in files_in(PORTS):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertFalse(imported.startswith(STRATEGY))

    def test_every_port_module_is_abstract(self) -> None:
        # A port file that defines a concrete class is drifting toward being an
        # adapter. Every class in trading.ports must be an ABC or a frozen
        # value type used in its signatures.
        for path in files_in(PORTS):
            if path.name == "__init__.py":
                continue
            with self.subTest(path=path.name):
                self.assertIn(
                    "abc",
                    imports_of(path),
                    f"{path.name} defines no abstract interface",
                )


class TestAdaptersRespectTheArrow(PurityCase):
    """Adapters may know the kernel. The kernel may not know them."""

    def test_adapters_import_only_stdlib_core_ports_and_adapters(self) -> None:
        for path in files_in(ADAPTERS):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertImportAllowed(path, imported, LAYER_RULES[ADAPTERS])

    def test_no_adapter_imports_a_strategy(self) -> None:
        for path in files_in(ADAPTERS):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertFalse(imported.startswith(STRATEGY))


class TestStrategyLayerCannotReachAVenue(PurityCase):
    """INVARIANT 3, on the import side.

    A strategy holding a concrete broker is the accident this catches; a
    strategy holding a *port type* for annotation is fine, because
    ``place_order`` still demands a token it cannot mint.
    """

    def test_strategy_imports_only_stdlib_core_and_ports(self) -> None:
        for path in files_in(STRATEGY):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertImportAllowed(path, imported, LAYER_RULES[STRATEGY])

    def test_no_strategy_module_imports_a_concrete_adapter(self) -> None:
        for path in files_in(STRATEGY):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertFalse(
                        imported.startswith(ADAPTERS),
                        f"{path.name} imports '{imported}'; a strategy must "
                        "never be able to name a venue (INVARIANT 3)",
                    )

    def test_no_strategy_module_imports_the_gateway(self) -> None:
        # Stronger than the layer rule: the gateway lives in core, so the layer
        # check would allow it. Strategy code must not be able to call submit().
        for path in files_in(STRATEGY):
            for imported in imports_of(path):
                with self.subTest(path=path.name, imported=imported):
                    self.assertNotEqual(
                        imported,
                        "trading.core.gateway",
                        f"{path.name} imports the execution gateway; strategies "
                        "propose intents and never execute them (INVARIANT 3)",
                    )


class TestNoThirdPartyAnywhere(PurityCase):
    """The Stage 1 constraint: standard library only, no installed packages."""

    def test_every_import_in_the_package_is_stdlib_or_first_party(self) -> None:
        for path in SOURCE_FILES:
            for imported in imports_of(path):
                root = top_level(imported)
                with self.subTest(path=path.name, imported=imported):
                    self.assertTrue(
                        root == "trading" or root in sys.stdlib_module_names,
                        f"{path.relative_to(REPO_ROOT)} imports third-party "
                        f"'{imported}'",
                    )

    def test_the_test_suite_is_also_stdlib_only(self) -> None:
        # A dependency hidden in the tests is still an installed dependency.
        for path in sorted((REPO_ROOT / "tests").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module]
                for name in names:
                    root = top_level(name)
                    with self.subTest(path=path.name, imported=name):
                        self.assertTrue(
                            root in {"trading", "tests"}
                            or root in sys.stdlib_module_names,
                            f"tests/{path.name} imports third-party '{name}'",
                        )

    def test_no_dependency_manifest_declares_a_requirement(self) -> None:
        # None of these exist yet. If one appears, it must stay empty for
        # Stage 1 -- this is the check that notices the day someone adds one.
        for name in ("requirements.txt", "requirements-dev.txt", "Pipfile"):
            manifest = REPO_ROOT / name
            with self.subTest(manifest=name):
                if not manifest.exists():
                    continue
                lines = [
                    line.strip()
                    for line in manifest.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                self.assertEqual(lines, [], f"{name} declares dependencies")


class TestNoInfrastructureModules(PurityCase):
    """No sockets, no subprocesses, no database drivers -- anywhere.

    Checked across the whole package rather than only the kernel, because
    :mod:`trading.adapters` documents that nothing under it performs network
    I/O. A stdlib ``import socket`` would pass the third-party check above and
    still break that promise, so it is banned by name.
    """

    def test_no_module_in_the_package_imports_infrastructure(self) -> None:
        for path in SOURCE_FILES:
            for imported in imports_of(path):
                root = top_level(imported)
                with self.subTest(path=path.name, imported=imported):
                    self.assertNotIn(
                        root,
                        BANNED_MODULES,
                        f"{path.relative_to(REPO_ROOT)} imports '{imported}'; "
                        "Stage 1 performs no network, process, or database I/O",
                    )

    def test_no_module_opens_a_socket_by_any_name(self) -> None:
        # Belt and braces: catch the string 'socket(' even if the import were
        # obtained some other way.
        for path in SOURCE_FILES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("socket.socket", source)
                self.assertNotIn("urlopen", source)

    def test_the_kernel_uses_no_dynamic_import_machinery(self) -> None:
        for layer in KERNEL_LAYERS:
            for path in files_in(layer):
                for imported in imports_of(path):
                    with self.subTest(path=path.name, imported=imported):
                        self.assertNotIn(
                            top_level(imported),
                            DYNAMIC_IMPORT_MODULES,
                            f"{path.name} imports '{imported}'; a dynamic "
                            "import would defeat this whole test module",
                        )

    def test_the_kernel_calls_no_dynamic_code(self) -> None:
        for layer in KERNEL_LAYERS:
            for path in files_in(layer):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        with self.subTest(path=path.name, call=node.func.id):
                            self.assertNotIn(
                                node.func.id,
                                DYNAMIC_CALLS,
                                f"{path.name} calls {node.func.id}() at line "
                                f"{node.lineno}",
                            )


class TestKernelIsCleanAtRuntime(PurityCase):
    """The static checks cannot see a lazy import. A fresh interpreter can.

    Each check runs in a subprocess so it observes a genuinely empty
    ``sys.modules``, uncontaminated by the test suite having already imported
    the adapters and the strategy layer.
    """

    def _probe(self, body: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", body],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"probe failed:\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )
        return result.stdout.strip()

    def test_importing_the_whole_kernel_pulls_in_no_adapter(self) -> None:
        leaked = self._probe(
            "import sys\n"
            "import trading.core.gateway, trading.core.sizing, trading.ports\n"
            "print(sorted(m for m in sys.modules "
            "if m.startswith(('trading.adapters', 'trading.strategy'))))\n"
        )
        self.assertEqual(leaked, "[]", f"kernel import pulled in {leaked}")

    def test_importing_the_whole_kernel_pulls_in_nothing_third_party(self) -> None:
        leaked = self._probe(
            "import sys\n"
            "baseline = set(sys.modules)\n"
            "import trading.core.gateway, trading.core.sizing, trading.ports\n"
            "added = {m.split('.', 1)[0] for m in set(sys.modules) - baseline}\n"
            "print(sorted(m for m in added "
            "if m != 'trading' and not m.startswith('_') "
            "and m not in sys.stdlib_module_names))\n"
        )
        self.assertEqual(leaked, "[]", f"kernel import pulled in {leaked}")

    def test_the_kernel_imports_with_no_installed_packages_reachable(self) -> None:
        # -S skips site-packages entirely: if the kernel needed anything
        # installed, this import fails outright.
        result = subprocess.run(
            [sys.executable, "-S", "-c", "import trading.core.gateway; print('ok')"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0, f"kernel needs site-packages: {result.stderr}"
        )
        self.assertEqual(result.stdout.strip(), "ok")

    def test_importing_the_kernel_opens_no_socket(self) -> None:
        # Replace socket.socket with something that fails loudly, then import.
        # A module doing network setup at import time trips this.
        self._probe(
            "import socket\n"
            "def _boom(*a, **k):\n"
            "    raise AssertionError('the kernel opened a socket at import time')\n"
            "socket.socket = _boom\n"
            "import trading.core.gateway, trading.ports\n"
            "print('ok')\n"
        )

    def test_the_adapters_layer_also_opens_no_socket(self) -> None:
        self._probe(
            "import socket\n"
            "def _boom(*a, **k):\n"
            "    raise AssertionError('an adapter opened a socket at import time')\n"
            "socket.socket = _boom\n"
            "import trading.adapters.memory, trading.strategy\n"
            "print('ok')\n"
        )

    def test_the_kernel_reads_no_environment_secret_at_import_time(self) -> None:
        # config.py imports os deliberately, but it must not read the
        # environment as a side effect of being imported.
        self._probe(
            "import os\n"
            "class Guard(dict):\n"
            "    def __getitem__(self, key):\n"
            "        raise AssertionError(f'read env {key} at import time')\n"
            "    def get(self, key, default=None):\n"
            "        raise AssertionError(f'read env {key} at import time')\n"
            "os.environ = Guard()\n"
            "import trading.core.config, trading.core.gateway\n"
            "print('ok')\n"
        )


class TestLayeringIsAcyclic(PurityCase):
    """The arrow points one way -- out of the kernel, never back into it.

    ``trading.core`` and ``trading.ports`` *do* import each other, deliberately:
    a port's signatures are written in core value types, and the kernel calls
    outward through the port interfaces. They are one unit, which is why both
    package docstrings describe them as a single kernel. So for cycle purposes
    they collapse to one node, and the edge that must not exist is
    kernel -> adapters or kernel -> strategy.
    """

    @staticmethod
    def _layer_edges() -> set[tuple[str, str]]:
        edges: set[tuple[str, str]] = set()
        for path in SOURCE_FILES:
            source_layer = layer_of(module_name_of(path))
            if source_layer is None:
                continue
            for imported in imports_of(path):
                target_layer = layer_of(imported)
                if target_layer and target_layer != source_layer:
                    edges.add((source_layer, target_layer))
        return edges

    @staticmethod
    def _collapse(layer: str) -> str:
        return "kernel" if layer in KERNEL_LAYERS else layer

    def test_no_pair_of_units_imports_both_ways(self) -> None:
        edges = {
            (self._collapse(source), self._collapse(target))
            for source, target in self._layer_edges()
        }
        edges = {(source, target) for source, target in edges if source != target}
        for source, target in sorted(edges):
            with self.subTest(edge=f"{source} -> {target}"):
                self.assertNotIn(
                    (target, source),
                    edges,
                    f"{source} and {target} import each other",
                )

    def test_the_kernel_halves_may_import_each_other(self) -> None:
        # Documenting the one intentional cycle, so that if a future refactor
        # removes it the removal is a deliberate choice rather than a surprise.
        edges = self._layer_edges()
        self.assertIn((CORE, PORTS), edges)
        self.assertIn((PORTS, CORE), edges)

    def test_the_expected_edges_are_present(self) -> None:
        # Asserting the shape positively, so a refactor that accidentally
        # flattens the layering is visible rather than silently "clean".
        edges = self._layer_edges()
        self.assertIn((CORE, PORTS), edges, "the kernel should call out via ports")
        self.assertIn((ADAPTERS, PORTS), edges, "adapters should implement ports")
        self.assertIn((STRATEGY, CORE), edges, "strategies should use core values")
        self.assertNotIn((PORTS, ADAPTERS), edges)
        self.assertNotIn((CORE, ADAPTERS), edges)
        self.assertNotIn((CORE, STRATEGY), edges)
        self.assertNotIn((PORTS, STRATEGY), edges)

    def test_each_kernel_module_imports_cleanly_on_its_own(self) -> None:
        # A cycle that only resolves because something else was imported first
        # is a latent ImportError. Each kernel module must stand alone in a
        # fresh interpreter.
        for path in files_in(CORE) + files_in(PORTS):
            name = module_name_of(path)
            with self.subTest(module=name):
                result = subprocess.run(
                    [sys.executable, "-c", f"import {name}"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode, 0, f"{name} failed to import: {result.stderr}"
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
