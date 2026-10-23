"""The pgschema binary: pinned version, downloaded once per machine and SHA256-checked;
`setup` also installs the scratch Postgres (scratch.py).

It lives in ~/.cache/postgresledger/ ($XDG_CACHE_HOME respected), so every project
on the machine shares one copy. PGL_PGSCHEMA points to another binary.
pgschema has no Windows build; on Windows run postgresledger inside WSL2.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION = "1.13.1"
# sha256 of the release assets, per <os>-<arch>
SHA256 = {
    "linux-amd64": "f726513e19d12f7667ba0b6a24e7a3a568e576708de030ef88f4ba3790e85946",
    "linux-arm64": "8202d3e2cba000872627472549dbfb4618d2b256b22e7144b4ff6ecd476f36c1",
    "darwin-amd64": "72d26778679407cec0816d1ec26fb123d2fd80120817977051c53e86b6afced5",
    "darwin-arm64": "15af7abf0b7e545a98452f2eed62650a7dc8aa6abcc7f54dd4f0f6e234238a43",
}
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "postgresledger"


def binary_path() -> Path:
    return cache_dir() / f"pgschema-{VERSION}"


def target() -> str:
    """This machine as pgschema names its builds, e.g. linux-amd64."""
    os_name = platform.system().lower()
    if os_name == "windows":
        sys.exit("pgschema has no Windows build - run postgresledger inside WSL2")
    architecture = ARCH.get(platform.machine().lower())
    build = f"{os_name}-{architecture}"
    if build not in SHA256:
        sys.exit(f"no pgschema build for {platform.system()} {platform.machine()}")
    return build


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install() -> Path:
    """Download the pinned pgschema unless the right one is already there."""
    build = target()
    binary = binary_path()
    expected_checksum = SHA256[build]
    if binary.exists() and sha256(binary) == expected_checksum:
        print(f"ok       pgschema {VERSION} ({build}) already in {binary}")
        return binary
    url = f"https://github.com/pgplex/pgschema/releases/download/v{VERSION}/pgschema-{VERSION}-{build}"
    print(f"download {url}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    # downloaded next to the binary and renamed only when complete and checked
    partial_binary = binary.with_suffix(".part")
    with urllib.request.urlopen(url) as response:
        partial_binary.write_bytes(response.read())
    actual_checksum = sha256(partial_binary)
    if actual_checksum != expected_checksum:
        partial_binary.unlink()
        sys.exit(f"pgschema checksum mismatch: expected {expected_checksum}, got {actual_checksum}")
    partial_binary.chmod(0o755)
    partial_binary.replace(binary)
    print(f"ok       pgschema {VERSION} ({build}) -> {binary}")
    return binary


def locate() -> str:
    """PGL_PGSCHEMA, then the downloaded binary, then pgschema in PATH."""
    if configured_binary := os.environ.get("PGL_PGSCHEMA"):
        return configured_binary
    if binary_path().exists():
        return str(binary_path())
    if binary_in_path := shutil.which("pgschema"):
        return binary_in_path
    sys.exit("pgschema not found - run `pgl setup` to download it")


def check_tool(name: str, version_arguments: list[str], hint: str, required: bool = True) -> bool:
    """Print whether the tool is installed (with its version); False only when a required tool is missing."""
    if not shutil.which(name):
        status = "MISSING" if required else "missing"
        print(f"{status}  {name} - {hint}")
        return not required
    result = subprocess.run([name, *version_arguments], capture_output=True, text=True, check=False)
    output_lines = result.stdout.strip().splitlines()
    version = output_lines[0] if output_lines else name
    print(f"ok       {version}")
    return True


def setup() -> int:
    """Download pgschema and the scratch Postgres, check the other tools postgresledger needs."""
    from . import scratch

    print(f"ok       Python {platform.python_version()}")
    install()
    scratch.install()
    psql_found = check_tool("psql", ["--version"], "install the PostgreSQL client (postgresql-client / libpq)")
    return 0 if psql_found else 1
