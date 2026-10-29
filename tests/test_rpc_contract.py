# SPDX-License-Identifier: BSD-3-Clause
# Shared from DeckyKit (shared/tests/test_rpc_contract.py): change it there, then `kit sync`.

"""The panel calls only RPCs main.py defines, each with a number of arguments the method accepts.

A mismatch is not caught by TypeScript or by the panel tests (they answer with stand-ins): Decky would answer the
call with a TypeError at runtime. Every `callable<[args], R>("name")` in src/ (also through a local wrapper such as
`const rpc = <A, R>(name: string) => ... callable<A, R>(name)`, a `type X = [...]` alias and `...X` spreads) is
checked against the public coroutine methods of main.Plugin, whose signatures dk_rpc.boundary keeps. The name may
stand on its own line, with a trailing comma or before a wrapper's further arguments (Prettier's layout).

Sources: DeckyVibranceHDR tests/rpc-contract.test.mjs (name and argument count), DeckyKit overview (callable names).
Guarantees: an unknown name fails; an argument list whose shortest or longest form the method cannot take fails
(optional `x?: T` elements and a backend default both counted); an argument list that cannot be read (not a tuple
literal or a known alias) fails instead of passing silently; at least one call must be found when src/ exists.
Deliberately not here: argument types (Python has none to compare), calls made with a computed name.
"""

import importlib
import inspect
import os
import re
import sys
import unittest

import _harness

ROOT = _harness.ROOT
CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*<")
ALIAS = re.compile(r"\btype\s+([A-Za-z_$][\w$]*)\s*=\s*\[")
OPEN, CLOSE = "<([{", ">)]}"


def _close(text, start):
    """Index just past the bracket group opening at text[start], or -1."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c in "\"'`":
            end = text.find(c, i + 1)
            if end < 0:
                return -1
            i = end
        elif c == "=" and text[i + 1:i + 2] == ">":
            i += 1  # an arrow, not a bracket
        elif c in OPEN:
            depth += 1
        elif c in CLOSE:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _split(inner):
    """Top-level comma-separated parts of a bracket group's inside."""
    parts, depth, start = [], 0, 0
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "=" and inner[i + 1:i + 2] == ">":
            i += 1
        elif c in OPEN:
            depth += 1
        elif c in CLOSE:
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
        i += 1
    parts.append(inner[start:])
    return [p.strip() for p in parts if p.strip()]


def _arity(tuple_type, aliases, seen=()):
    """(fewest, most) arguments a tuple type allows; None when it cannot be read."""
    tuple_type = tuple_type.strip()
    if tuple_type in aliases and tuple_type not in seen:
        return _arity(aliases[tuple_type], aliases, (*seen, tuple_type))
    if not tuple_type.startswith("[") or _close(tuple_type, 0) != len(tuple_type):
        return None
    low = high = 0
    for part in _split(tuple_type[1:-1]):
        if part.startswith("..."):
            inner = _arity(part[3:], aliases, seen)
            if inner is None:
                return None
            low, high = low + inner[0], high + inner[1]
            continue
        optional = bool(re.match(r"^[A-Za-z_$][\w$]*\?\s*:", part)) or part.endswith("?")
        low, high = low + (0 if optional else 1), high + 1
    return low, high


def frontend_calls(src):
    """[(file, name, (fewest, most) or None)] for every RPC the panel calls through callable or a local wrapper."""
    texts = {}
    for folder, _dirs, files in os.walk(src):
        for name in files:
            if name.endswith((".ts", ".tsx")) and not name.endswith(".d.ts"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    texts[os.path.relpath(os.path.join(folder, name), src)] = f.read()
    aliases = {}
    for text in texts.values():
        for m in ALIAS.finditer(text):
            end = _close(text, m.end() - 1)
            if end > 0:
                aliases[m.group(1)] = text[m.end() - 1:end]
    wrappers = {"callable"} | {m.group(1) for text in texts.values()
                               for m in re.finditer(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*<", text)
                               if "callable<" in text[m.end():m.end() + 300]}
    calls = []
    for file, text in texts.items():
        for m in CALL.finditer(text):
            if m.group(1) not in wrappers:
                continue
            end = _close(text, m.end() - 1)
            name = re.match(r"\s*\(\s*([\"'])([A-Za-z_][\w]*)\1\s*[,)]", text[end:]) if end > 0 else None
            if not name:
                continue  # the wrapper's own body, or a computed name
            types = _split(text[m.end():end - 1])
            calls.append((file, name.group(2), _arity(types[0], aliases) if types else None))
    return calls


def backend_methods(plugin_class):
    """{name: (required, most)} of the public coroutine methods; most is None for *args."""
    methods = {}
    for name, value in vars(plugin_class).items():
        if name.startswith("_") or not inspect.iscoroutinefunction(inspect.unwrap(value)):
            continue
        params = list(inspect.signature(value).parameters.values())[1:]  # self; boundary keeps the signature
        if any(p.kind == p.VAR_POSITIONAL for p in params):
            most = None
        else:
            most = sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params)
        required = sum(p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                       for p in params)
        methods[name] = (required, most)
    return methods


def problems(src, plugin_class):
    calls = frontend_calls(src)
    if not calls:
        return ["no callable(...) found in src/: the contract cannot be checked"]
    methods = backend_methods(plugin_class)
    found = []
    for file, name, arity in calls:
        if name not in methods:
            found.append(f"{file}: {name} is not a public async method of Plugin in main.py")
        elif arity is None:
            found.append(f"{file}: {name}: the argument list is not a tuple type this test can read")
        else:
            required, most = methods[name]
            if arity[0] < required or (most is not None and arity[1] > most):
                found.append(f"{file}: {name} is called with {arity[0]}..{arity[1]} arguments, "
                             f"main.py takes {required}..{'any' if most is None else most}")
    return found


class RpcContractTest(unittest.TestCase):
    def test_the_panel_calls_only_what_main_py_defines(self):
        src = os.path.join(ROOT, "src")
        if not (os.path.isfile(os.path.join(ROOT, "main.py")) and os.path.isdir(src)):
            self.skipTest("not inside a plugin (DeckyKit's own copy checks itself below)")
        main = importlib.import_module("main")
        self.assertEqual(problems(src, main.Plugin), [])


class ContractCheckTest(unittest.TestCase):
    """The check itself, on a made-up panel."""

    class Plugin:
        async def get_state(self):
            pass

        async def set_value(self, key, value=None):
            pass

        async def any_args(self, *args):
            pass

        async def _main(self):
            pass

        def helper(self):
            pass

    def check(self, source):
        import tempfile
        with tempfile.TemporaryDirectory() as src:
            with open(os.path.join(src, "api.ts"), "w", encoding="utf-8") as f:
                f.write(source)
            return problems(src, self.Plugin)

    def test_matching_calls_pass(self):
        self.assertEqual(self.check(
            'type Two = [key: string, value: number];\n'
            'const a = callable<[], Result<State>>("get_state");\n'
            'const b = callable<[string], Result<{ ok: boolean; f: () => void }>>("set_value");\n'
            'const c = callable<Two, R>("set_value");\n'
            'const d = callable<[key: string, value?: number], R>("set_value");\n'
            'const e = callable<[string, ...Two], R>("any_args");\n'
            'const rpc = <A extends unknown[], R>(name: string) => { const call = callable<A, R>(name); };\n'
            'const f = rpc<[string, number], R>("set_value");\n'
            'const g = callable<\n  [key: string],\n  R\n>(\n  "set_value",\n);\n'
            'const h = rpc<[], R>("get_state", { slow: true });\n'), [])

    def test_unknown_names_and_wrong_counts_fail(self):
        found = self.check('const a = callable<[], R>("missing");\n'
                           'const b = callable<[], R>("set_value");\n'
                           'const c = callable<[string, number, boolean], R>("set_value");\n'
                           'const d = callable<[number], R>("_main");\n'
                           'const e = callable<[number], R>("helper");\n'
                           'const rpc = <A, R>(name: string) => callable<A, R>(name);\n'
                           'const f = rpc<[], R>("get_states");\n'
                           'const g = callable<[], R>(\n  "unknown_rpc",\n);\n')
        self.assertEqual(len(found), 7, found)
        self.assertTrue(any("missing" in p for p in found) and any("get_states" in p for p in found)
                        and any("unknown_rpc" in p for p in found))

    def test_an_unreadable_argument_list_fails(self):
        self.assertEqual(len(self.check('const a = callable<Unknown, R>("get_state");\n')), 1)
        self.assertEqual(self.check("const x = 1;\n"), ["no callable(...) found in src/: the contract cannot be "
                                                        "checked"])


if __name__ == "__main__":
    sys.exit(unittest.main())
