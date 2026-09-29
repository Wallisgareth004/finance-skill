#!/usr/bin/env python3
"""test_no_network.py — the pipeline needs no API key, no account and no network.

A4, 2026-09-27. Privacy by construction, not by promise: this reads every
pipeline and backtest module's syntax tree and fails if any of them
  · imports a network library (socket, urllib, http, requests, httpx, …)
  · reads an environment variable that is not one of the skill's own
    FINANCE_* settings — the place an API key would have to come from
Everything the skill reads is a local file the owner downloaded by hand.
"""

from __future__ import annotations

import ast
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = sorted(
    glob.glob(os.path.join(ROOT, "scripts", "finance_pipeline", "*.py"))
    + glob.glob(os.path.join(ROOT, "backtest", "*.py"))
    + glob.glob(os.path.join(ROOT, "backtest", "synth", "*.py"))
)
NETWORK = {"socket", "ssl", "urllib", "http", "requests", "httpx", "aiohttp",
           "smtplib", "ftplib", "telnetlib", "xmlrpc", "websocket", "websockets"}


def _tree(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _constants(tree):
    """Module-level NAME = "literal" assignments, so os.environ.get(MODEL_ENV)
    is checked against the value MODEL_ENV actually holds."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
    return out


def _env_names(tree):
    """Names passed to os.environ.get / os.environ[...] / os.getenv, with
    module-level constants resolved to their literal."""
    consts = _constants(tree)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            is_get = f.attr == "get" and ast.unparse(f.value) == "os.environ"
            if (is_get or ast.unparse(f) == "os.getenv") and node.args:
                names.append(node.args[0])
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            names.append(node.slice)
    return [consts.get(n.id, n) if isinstance(n, ast.Name) else n for n in names]


class TestNoNetwork(unittest.TestCase):
    def test_there_are_modules_to_check(self):
        self.assertGreaterEqual(len(MODULES), 10)

    def test_no_module_imports_a_network_library(self):
        for path in MODULES:
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Import):
                    roots = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    roots = {node.module.split(".")[0]}
                else:
                    continue
                with self.subTest(module=os.path.basename(path)):
                    self.assertFalse(roots & NETWORK, f"imports {roots & NETWORK}")


class TestNoSecretsFromTheEnvironment(unittest.TestCase):
    def test_only_the_skills_own_settings_are_read(self):
        for path in MODULES:
            for arg in _env_names(_tree(path)):
                with self.subTest(module=os.path.basename(path)):
                    self.assertIsInstance(arg, ast.Constant, "env name must be a literal")
                    self.assertTrue(
                        str(arg.value).startswith("FINANCE_"),
                        f"reads {arg.value!r}: only FINANCE_* settings are allowed",
                    )


if __name__ == "__main__":
    unittest.main()
