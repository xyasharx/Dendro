#!/usr/bin/python3
"""Delete one validated RPM repository configuration file as root.

This program is intentionally not a package-manager command runner.  Its only
accepted operation is ``delete <absolute .repo file>`` directly inside one of
Dendro's supported repository directories.  It uses a trusted directory file
descriptor and ``unlinkat``-style deletion through ``dir_fd`` to avoid path
traversal and symlink races after Polkit authorization.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import PurePosixPath


_ALLOWED_DIRECTORIES = (
    "/etc/yum.repos.d",
    "/etc/dnf/repos.d",
)


def _fail(message: str, exit_code: int = 2) -> int:
    print(f"dendro-repo-helper: {message}", file=sys.stderr)
    return exit_code


def _validate_target(value: str) -> tuple[str, str]:
    if not value or "\x00" in value:
        raise ValueError("repository file path must be non-empty text")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.suffix.casefold() != ".repo":
        raise ValueError("target must be an absolute .repo file")
    if path.name in {"", ".", ".."}:
        raise ValueError("target must name a repository file")
    parent = str(path.parent)
    if parent not in _ALLOWED_DIRECTORIES:
        raise ValueError("target must be directly inside /etc/yum.repos.d or /etc/dnf/repos.d")
    if str(path) != value:
        raise ValueError("target path must already be normalized")
    return parent, path.name


def _open_trusted_directory(directory: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(directory, flags)
    directory_status = os.fstat(descriptor)
    if not stat.S_ISDIR(directory_status.st_mode):
        os.close(descriptor)
        raise OSError("repository parent is not a directory")
    if directory_status.st_uid != 0:
        os.close(descriptor)
        raise PermissionError("repository parent is not owned by root")
    if directory_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(descriptor)
        raise PermissionError("repository parent is writable by a non-root group or user")
    return descriptor


def delete_repository_file(value: str) -> int:
    if os.geteuid() != 0:
        return _fail("must be run as root", 1)

    try:
        directory, filename = _validate_target(value)
    except ValueError as exc:
        return _fail(str(exc))

    descriptor: int | None = None
    try:
        descriptor = _open_trusted_directory(directory)
        target_status = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
        if not (stat.S_ISREG(target_status.st_mode) or stat.S_ISLNK(target_status.st_mode)):
            return _fail("target is not a regular file or symbolic link", 1)
        if target_status.st_uid != 0:
            return _fail("target is not owned by root", 1)
        if target_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return _fail("target is writable by a non-root group or user", 1)

        # unlink with dir_fd removes the directory entry validated above.  It
        # does not follow a final symlink and cannot escape the opened parent.
        os.unlink(filename, dir_fd=descriptor)
    except FileNotFoundError:
        return _fail("target repository file does not exist", 1)
    except (OSError, PermissionError) as exc:
        return _fail(f"could not delete repository file: {exc}", 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)

    print(f"Deleted repository configuration: {directory}/{filename}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dendro-repo-helper",
        description="Narrow privileged helper for deleting one Dendro repository configuration file.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    delete = subcommands.add_parser("delete", help="delete one validated .repo file")
    delete.add_argument("repository_file", metavar="FILE")
    arguments = parser.parse_args(argv)

    if arguments.command == "delete":
        return delete_repository_file(arguments.repository_file)
    return _fail("unsupported operation")


if __name__ == "__main__":
    raise SystemExit(main())
