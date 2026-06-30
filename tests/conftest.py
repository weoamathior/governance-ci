"""Shared test setup.

Puts scripts/ on the import path and stubs the `anthropic` SDK so the engine
modules import without the real package. These are pure unit tests: they never call
the model, `gh`, or the network (the live model path is covered by the evals harness).
"""
import pathlib
import sys
import types

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

# evaluate.py does `import anthropic` at module top. Provide a no-op stand-in so the
# import succeeds in CI without installing the SDK; no test constructs a real client.
if "anthropic" not in sys.modules:
    _fake = types.ModuleType("anthropic")

    class _Anthropic:  # pragma: no cover - never exercised by unit tests
        def __init__(self, *a, **k):
            pass

    _fake.Anthropic = _Anthropic
    sys.modules["anthropic"] = _fake
