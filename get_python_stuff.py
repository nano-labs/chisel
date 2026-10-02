from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Set

# Runs inside the target interpreter. It only lists module names; pkgutil
# and importlib.metadata read directory listings and package metadata,
# they don't import the packages.
_ENV_PROBE = r"""
import json, os, pkgutil, sys, sysconfig
stdlib = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)
if not getattr(sys, "stdlib_module_names", None):  # Python < 3.10
    lib = sysconfig.get_paths()["stdlib"]
    stdlib.update(m.name for m in pkgutil.iter_modules([lib, os.path.join(lib, "lib-dynload")]))
stdlib.add("__future__")
installed = set()
try:
    from importlib.metadata import packages_distributions
    installed.update(packages_distributions())
except Exception:
    pass
paths = {sysconfig.get_paths()[k] for k in ("purelib", "platlib")}
try:
    import site
    paths.update(site.getsitepackages())
    paths.add(site.getusersitepackages())
except Exception:
    pass
installed.update(m.name for m in pkgutil.iter_modules([p for p in paths if os.path.isdir(p)]))
print(json.dumps({"python": sys.executable, "version": sys.version.split()[0],
                  "stdlib": sorted(stdlib), "installed": sorted(installed - stdlib)}))
"""


@dataclass
class Environment:
    python: str
    version: str
    stdlib: Set[str]
    installed: Set[str]


def probe_environment(python: Optional[str] = None) -> Environment:
    exe = python or sys.executable
    try:
        proc = subprocess.run(
            [exe, "-c", _ENV_PROBE],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        return Environment(
            data["python"], data["version"], set(data["stdlib"]), set(data["installed"])
        )
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        print(
            f"warning: could not inspect {exe} ({exc}); "
            "falling back to this interpreter's stdlib list",
            file=sys.stderr,
        )
        stdlib = set(getattr(sys, "stdlib_module_names", ())) | set(
            sys.builtin_module_names
        )
        return Environment(
            sys.executable, sys.version.split()[0], stdlib | {"__future__"}, set()
        )
