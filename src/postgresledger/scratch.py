"""A throwaway Postgres on this machine, for the work that needs a scratch database.

Building "every migration applied", generating a migration, checking drift,
and capture need a database postgresledger may drop and recreate. That never
happens on a real server: each command starts its own Postgres in a temporary
folder on a free local port, and deletes it when the command ends.

The server binaries are a pinned build from Maven Central (io.zonky.test.postgres,
relocatable), downloaded once per machine into ~/.cache/postgresledger/ and SHA256-checked.
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import Connection
from .pgschema import cache_dir, sha256

VERSION = "17.11.0"
# sha256 of the jars, per platform (cross-checked with Maven Central's .sha1)
SHA256 = {
    "linux-amd64": "0dd7b72b6f335b8ecfb355fa24c5781e8a93edd09880bb77eb52ebbf29b3e96d",
    "linux-arm64v8": "8b042e0ea418b1927d95399207950da0734d27fe01e29503ade3bdc464ad4c2b",
    "darwin-amd64": "d464ff178e9860ba204662ac23fa547504b7fd392392ff2fb92e3fd73b5bdb64",
    "darwin-arm64v8": "a1c2786acb0c398f9b2d76806fc52f5dc8b222cbc8e9383a9b9702084daaf3a5",
}
# <os>-<arch> -> the name of the Maven Central build
BUILD = {
    "linux-amd64": "linux-amd64",
    "linux-arm64": "linux-arm64v8",
    "darwin-amd64": "darwin-amd64",
    "darwin-arm64": "darwin-arm64v8",
}
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def install_dir() -> Path:
    return cache_dir() / f"postgres-{VERSION}"


def target() -> str:
    """This machine as Maven Central names the Postgres builds, e.g. linux-arm64v8."""
    os_name = platform.system().lower()
    architecture = ARCH.get(platform.machine().lower())
    platform_name = f"{os_name}-{architecture}"
    if platform_name not in BUILD:
        sys.exit(f"no Postgres build for {platform.system()} {platform.machine()} (Windows: use WSL2)")
    return BUILD[platform_name]


def install() -> Path:
    """Download and unpack the pinned Postgres server unless it is already there."""
    destination = install_dir()
    if (destination / "bin" / "pg_ctl").exists():
        print(f"ok       Postgres {VERSION} (scratch server) already in {destination}")
        return destination
    build = target()
    url = (f"https://repo1.maven.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-{build}/{VERSION}/"
           f"embedded-postgres-binaries-{build}-{VERSION}.jar")
    print(f"download {url}")
    with urllib.request.urlopen(url) as response:
        jar = response.read()
    with tempfile.NamedTemporaryFile() as jar_file:
        jar_file.write(jar)
        jar_file.flush()
        actual_checksum = sha256(Path(jar_file.name))
    expected_checksum = SHA256[build]
    if actual_checksum != expected_checksum:
        sys.exit(f"Postgres checksum mismatch: expected {expected_checksum}, got {actual_checksum}")

    # the jar holds one .txz archive with the server; unpacked next to the destination,
    # renamed only when complete
    partial_destination = destination.with_name(destination.name + ".part")
    shutil.rmtree(partial_destination, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(jar)) as jar_archive:
        archive_names = [name for name in jar_archive.namelist() if name.endswith(".txz")]
        server_archive = jar_archive.read(archive_names[0])
    with tarfile.open(fileobj=io.BytesIO(server_archive), mode="r:xz") as tar:
        # Python versions with extraction filters: keep permissions, block paths outside the folder
        if hasattr(tarfile, "tar_filter"):
            tar.extractall(partial_destination, filter="tar")
        else:
            tar.extractall(partial_destination)
    partial_destination.replace(destination)
    print(f"ok       Postgres {VERSION} (scratch server) -> {destination}")
    return destination


def free_port() -> int:
    """A local port nothing listens on: the operating system picks it when binding to port 0."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_server_program(command: list[str]) -> None:
    """Run initdb or pg_ctl; if it fails, show its output and stop pgl."""
    program = Path(command[0]).name
    try:
        subprocess.run(command, text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as error:
        # the program ran and failed: its own output says why
        program_output = (error.stdout + error.stderr).strip()
        sys.exit(f"scratch Postgres: {program} failed:\n{program_output}")
    except OSError as error:
        # the program could not run at all, e.g. the binary is missing
        sys.exit(f"scratch Postgres: could not run {program}: {error}")


@contextmanager
def server() -> Iterator[Connection]:
    """Start a throwaway Postgres, yield a connection to its `postgres` database, delete it afterwards."""
    bin_dir = install_dir() / "bin"
    if not (bin_dir / "pg_ctl").exists():
        sys.exit("the scratch Postgres is not installed - run `pgl setup`")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("the scratch Postgres cannot run as root - run pgl as a normal user")
    with tempfile.TemporaryDirectory(prefix="postgresledger-") as temporary_dir:
        data_dir = Path(temporary_dir) / "data"
        log_file = Path(temporary_dir) / "server.log"
        port = free_port()
        run_server_program([
            str(bin_dir / "initdb"),
            "-D", str(data_dir),
            "-U", "postgres",
            "-A", "trust",  # no passwords: only this machine can reach it
            "-E", "UTF8",
            "--locale=C",
            "--no-sync",  # a throwaway database does not need to survive a crash
        ])
        server_options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off"
        run_server_program([
            str(bin_dir / "pg_ctl"),
            "-D", str(data_dir),
            "-l", str(log_file),
            "-w", "start",
            "-o", server_options,
        ])
        try:
            yield Connection("127.0.0.1", port, "postgres", "", "postgres", "disable")
        finally:
            stop_command = [str(bin_dir / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "-w", "stop"]
            subprocess.run(stop_command, capture_output=True, check=False)
