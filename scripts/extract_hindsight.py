"""Extract useful info from Hindsight .pyc files and client API."""
import importlib.util
import dis
import marshal
import os
import sys
import types

HINDSIGHT_BASE = "C:/Users/Administrator/AppData/Local/hermes/hermes-agent/.venv/Lib/site-packages/hindsight_api"

print("=" * 60)
print("Hindsight .pyc analysis — extracting signatures & constants")
print("=" * 60)

# Walk pycache
for root, dirs, files in os.walk(os.path.join(HINDSIGHT_BASE, "__pycache__")):
    for f in sorted(files):
        if not f.endswith(".pyc"):
            continue
        pyc_path = os.path.join(root, f)
        mod_name = f.replace(".cpython-313.pyc", "").replace(".pyc", "")
        rel_path = os.path.relpath(pyc_path, HINDSIGHT_BASE)
        print(f"\n--- {rel_path} ---")

        try:
            with open(pyc_path, "rb") as fh:
                # Skip magic number (4) + flags (4) + timestamp/size (8) 
                fh.read(16)
                code = marshal.load(fh)

            # Extract all constants that are strings (docstrings, etc.)
            string_consts = []
            for const in code.co_consts:
                if isinstance(const, str) and len(const) > 10:
                    string_consts.append(const.strip()[:200])

            if string_consts:
                print("  Docstrings/constants:")
                for s in string_consts[:5]:
                    print(f"    {s}")

            # Extract function/class names
            names = list(code.co_names)
            if names:
                print(f"  Names: {', '.join(names[:20])}")

            # Look for nested code objects (functions/methods)
            for const in code.co_consts:
                if isinstance(const, types.CodeType):
                    print(f"  Function: {const.co_name}({', '.join(const.co_varnames[:const.co_argcount])})")
                    # Get docstring
                    if const.co_consts and isinstance(const.co_consts[0], str):
                        doc = const.co_consts[0].strip()[:150]
                        if doc:
                            print(f"    doc: {doc}")

        except Exception as e:
            print(f"  Error: {e}")

# Also read client API models
print("\n" + "=" * 60)
print("Hindsight Client API — Data Models")
print("=" * 60)

CLIENT_BASE = "C:/Users/Administrator/AppData/Local/hermes/hermes-agent/.venv/Lib/site-packages/hindsight_client_api/models"
for f in sorted(os.listdir(CLIENT_BASE)):
    if f.endswith(".py") and not f.startswith("__"):
        path = os.path.join(CLIENT_BASE, f)
        with open(path) as fh:
            content = fh.read()
        # Extract class definitions and key attributes
        lines = content.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("class ") or "attribute_map" in stripped or ": " in stripped and ("str" in stripped or "int" in stripped or "float" in stripped or "bool" in stripped or "list" in stripped):
                if not stripped.startswith("#") and not stripped.startswith('"""'):
                    print(f"  {f}: {stripped[:120]}")
