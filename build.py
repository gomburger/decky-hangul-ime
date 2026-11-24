#!/usr/bin/env python3
"""Package the plugin for Decky's "Install Plugin from ZIP File".

Runs the tests, checks that dist/index.js parses in Steam's own engine when Steam
is reachable, then writes decky-hangul-ime-<version>.zip, version from
package.json, to the folder above the project, and checks that every file in the
zip matches its source. `build.py DIR` writes to DIR instead.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
NAME = "decky-hangul-ime"
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent
FILES = ["package.json", "main.py", "plugin.json", "LICENSE", "README.md", "dist/index.js"] + sorted(
    f"py_modules/{path.name}" for path in (ROOT / "py_modules").glob("*.py")
)
# A helper kept beside the project, not shipped: evaluates JavaScript in a Steam page.
CEF_EVAL = ROOT.parent / "decky-hangul-ime-tools" / "cef_eval.py"


def run_tests():
    environment = {key: value for key, value in os.environ.items() if key != "DISPLAY"}
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-q"],
        cwd=ROOT / "tests", env=environment, capture_output=True, text=True,
    )
    if result.returncode:
        sys.exit(result.stderr)
    ran = [line for line in result.stderr.splitlines() if line.startswith("Ran ")]
    print(f"tests: {ran[-1] if ran else 'no output'}, OK")


def check_script():
    """Parse dist/index.js in Steam's engine, which is what runs it."""
    if not CEF_EVAL.exists():
        print("parse check: skipped, no CEF helper")
        return
    source = (ROOT / "dist/index.js").read_text(encoding="utf-8")
    source = "\n".join(line for line in source.split("\n") if not line.startswith("export "))
    expression = "(()=>{try{new Function(%s);return 'parse ok'}catch(e){return 'ERR '+e}})()" % json.dumps(source)
    result = subprocess.run(
        [sys.executable, str(CEF_EVAL), "SharedJSContext", expression], capture_output=True, text=True,
    )
    answer = result.stdout.strip()
    if "parse ok" in answer:
        print("parse check: ok")
    elif "ERR" in answer:
        sys.exit(f"parse check failed: {answer}")
    else:
        print("parse check: skipped, Steam not reachable")


def remove_caches():
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache)


def write_zip(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in FILES:
            archive.write(ROOT / file, f"{NAME}/{file}")


def check_zip(path, version):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if names != [f"{NAME}/{file}" for file in FILES]:
            sys.exit(f"unexpected files in {path}")
        for file in FILES:
            if archive.read(f"{NAME}/{file}") != (ROOT / file).read_bytes():
                sys.exit(f"{file} in {path} differs from the source")
        shipped = json.loads(archive.read(f"{NAME}/package.json"))["version"]
    if shipped != version:
        sys.exit(f"version {shipped} in {path}, expected {version}")


def main():
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    run_tests()
    check_script()
    remove_caches()
    OUT.mkdir(exist_ok=True)
    versioned = OUT / f"{NAME}-{version}.zip"
    write_zip(versioned)
    check_zip(versioned, version)
    print(f"{versioned}  {versioned.stat().st_size:,} bytes")
    print(f"{len(FILES)} files, version {version}")


if __name__ == "__main__":
    main()
