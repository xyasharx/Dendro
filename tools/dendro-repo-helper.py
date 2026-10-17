#!/usr/bin/python3
"""Perform one constrained RPM repository-file operation as root.

This helper is not a package-manager command runner. It accepts only two fixed
operations:

* ``delete <absolute .repo file>`` removes one validated file directly below a
  supported repository directory.
* ``add <generated filename>`` reads one bounded UTF-8 repository definition
  from standard input, validates a deliberately small allowlist of DNF5
  repository options, rejects duplicate repository IDs, and atomically creates
  a new root-owned file below ``/etc/yum.repos.d`` without overwriting one.

The unprivileged GUI downloads remote content before this helper is launched.
Consequently, the helper never performs network I/O, invokes DNF, invokes a
shell, or accepts an arbitrary executable or command line.
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import secrets
import signal
import stat
import sys
from pathlib import PurePosixPath
from urllib.parse import urlsplit


_ALLOWED_DIRECTORIES = (
    "/etc/yum.repos.d",
    "/etc/dnf/repos.d",
)
# The configured DNF5 defaults are scanned for repository-ID collisions before
# adding a file. DNF5 can be configured with a custom reposdir, which is owned
# by the system administrator and cannot safely be inferred by this fixed helper.
_KNOWN_REPOSITORY_DIRECTORIES = (
    "/etc/yum.repos.d",
    "/etc/dnf/repos.d",
    "/etc/distro.repos.d",
    "/usr/share/dnf5/repos.d",
)
_ADD_DIRECTORY = "/etc/yum.repos.d"
_MAX_CONFIGURATION_BYTES = 256 * 1024
_MAX_EXISTING_REPOSITORY_FILE_BYTES = 1024 * 1024
_MAX_REPOSITORY_SECTIONS = 64
_REPOSITORY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_ADD_FILENAME_PATTERN = re.compile(r"^dendro-[a-z0-9][a-z0-9._-]{0,120}\.repo$")
_INTEGER_PATTERN = re.compile(r"^[0-9]+$")
_METADATA_EXPIRE_PATTERN = re.compile(r"^(?:-1|never|[0-9]+(?:[smhdw])?)$", re.IGNORECASE)

# These options cover direct COPR files and ordinary signed third-party DNF5
# repositories. Deliberately excluding proxy credentials, local TLS paths,
# plugin controls, and arbitrary global options keeps a downloaded .repo file
# from becoming a carrier for unrelated privileged DNF configuration.
_ALLOWED_REPOSITORY_OPTIONS = frozenset(
    {
        "baseurl",
        "cost",
        "enabled",
        "enabled_metadata",
        "excludepkgs",
        "gpgcheck",
        "gpgkey",
        "includepkgs",
        "metadata_expire",
        "metalink",
        "mirrorlist",
        "module_hotfixes",
        "name",
        "pkg_gpgcheck",
        "priority",
        "repo_gpgcheck",
        "skip_if_unavailable",
        "type",
    }
)
_BOOLEAN_OPTIONS = frozenset(
    {
        "enabled",
        "enabled_metadata",
        "gpgcheck",
        "module_hotfixes",
        "pkg_gpgcheck",
        "repo_gpgcheck",
        "skip_if_unavailable",
    }
)
_INTEGER_OPTIONS = frozenset({"cost", "priority"})
_PACKAGE_FILTER_OPTIONS = frozenset({"includepkgs", "excludepkgs"})
_ALLOWED_REPOSITORY_TYPES = frozenset({"rpm-md", "rpm", "repomd", "rpmmd", "yum"})


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


def _validate_add_filename(value: str) -> str:
    if not isinstance(value, str) or not _ADD_FILENAME_PATTERN.fullmatch(value):
        raise ValueError("target filename must be a Dendro-generated .repo filename")
    return value


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


def _validate_https_url(value: str, label: str) -> None:
    if not value or len(value) > 4096:
        raise ValueError(f"{label} must be a non-empty HTTPS URL")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains whitespace or control characters")
    if any(character in value for character in ('\\', '<', '>', '"', '`')):
        raise ValueError(f"{label} contains unsupported URL characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} is malformed: {exc}") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ValueError(f"{label} must use HTTPS and include a hostname")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials or a fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{label} has an invalid port")


def _validate_url_list(value: str, label: str) -> None:
    values = value.split()
    if not values:
        raise ValueError(f"{label} must contain at least one HTTPS URL")
    for item in values:
        _validate_https_url(item, label)


def _parse_boolean(value: str, label: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "yes", "true", "on"}:
        return True
    if normalized in {"0", "no", "false", "off"}:
        return False
    raise ValueError(f"{label} must be a boolean value")


def _validate_integer(value: str, label: str) -> None:
    normalized = value.strip()
    if not _INTEGER_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} must be a non-negative integer")
    if int(normalized) > 2_147_483_647:
        raise ValueError(f"{label} exceeds DNF5's supported integer range")


def _validate_name(value: str, section: str) -> None:
    if not value or len(value) > 512:
        raise ValueError(f"{section}.name must contain between 1 and 512 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{section}.name contains a control character")


def _validate_package_filter(value: str, label: str) -> None:
    if not value or len(value) > 4096:
        raise ValueError(f"{label} must contain between 1 and 4096 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")


def _parse_repository_configuration(payload: bytes) -> tuple[bytes, tuple[str, ...]]:
    """Return canonical bytes and new repository IDs for a safe repo definition."""
    if not payload:
        raise ValueError("repository configuration is empty")
    if len(payload) > _MAX_CONFIGURATION_BYTES:
        raise ValueError(f"repository configuration exceeds {_MAX_CONFIGURATION_BYTES // 1024} KiB")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("repository configuration is not valid UTF-8") from exc
    if not text.strip():
        raise ValueError("repository configuration is empty")
    if "\x00" in text or any(ord(character) < 32 and character not in "\t\r\n" for character in text):
        raise ValueError("repository configuration contains unsupported control characters")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(line.strip().casefold() == "[default]" for line in text.splitlines()):
        raise ValueError("repository configuration must not define a DEFAULT section")

    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
        delimiters=("=", ":"),
    )
    parser.optionxform = str.casefold
    try:
        parser.read_string(text)
    except (configparser.Error, ValueError) as exc:
        raise ValueError(f"repository configuration is not valid INI: {exc}") from exc

    if parser.defaults():
        raise ValueError("repository configuration must not define a DEFAULT section")
    sections = parser.sections()
    if not sections:
        raise ValueError("repository configuration contains no repository sections")
    if len(sections) > _MAX_REPOSITORY_SECTIONS:
        raise ValueError(f"repository configuration contains more than {_MAX_REPOSITORY_SECTIONS} sections")
    normalized_section_ids = [section.casefold() for section in sections]
    if len(set(normalized_section_ids)) != len(normalized_section_ids):
        raise ValueError("repository configuration contains duplicate repository IDs")

    for section in sections:
        if section.casefold() == "main":
            raise ValueError("repository configuration must not define a [main] section")
        if section.casefold() == "default":
            raise ValueError("repository configuration must not define a DEFAULT section")
        if not _REPOSITORY_ID_PATTERN.fullmatch(section):
            raise ValueError(f"repository ID {section!r} contains unsupported characters")
        options = dict(parser.items(section, raw=True))
        unsupported = sorted(set(options).difference(_ALLOWED_REPOSITORY_OPTIONS))
        if unsupported:
            raise ValueError(
                f"repository {section!r} contains unsupported option(s): {', '.join(unsupported)}"
            )
        for option_name, option_value in options.items():
            if "\n" in option_value or "\r" in option_value:
                raise ValueError(f"repository option {option_name!r} in {section!r} must be a single line")

        source_options = tuple(
            (name, options.get(name, "").strip())
            for name in ("baseurl", "metalink", "mirrorlist")
        )
        if not any(value for _name, value in source_options):
            raise ValueError(f"repository {section!r} must define baseurl, metalink, or mirrorlist")
        for option_name, option_value in source_options:
            if option_value:
                _validate_url_list(option_value, f"{section}.{option_name}")

        for option_name in _BOOLEAN_OPTIONS.intersection(options):
            _parse_boolean(options[option_name], f"{section}.{option_name}")
        for option_name in _INTEGER_OPTIONS.intersection(options):
            _validate_integer(options[option_name], f"{section}.{option_name}")
        for option_name in _PACKAGE_FILTER_OPTIONS.intersection(options):
            _validate_package_filter(options[option_name], f"{section}.{option_name}")

        if "name" in options:
            _validate_name(options["name"], section)
        if "type" in options and options["type"].strip().casefold() not in _ALLOWED_REPOSITORY_TYPES:
            raise ValueError(f"{section}.type must be an rpm-md alias")
        if "metadata_expire" in options and not _METADATA_EXPIRE_PATTERN.fullmatch(options["metadata_expire"].strip()):
            raise ValueError(f"{section}.metadata_expire has an unsupported value")
        if "gpgkey" in options:
            _validate_url_list(options["gpgkey"].strip(), f"{section}.gpgkey")

        signature_options = tuple(name for name in ("gpgcheck", "pkg_gpgcheck") if name in options)
        if not signature_options:
            raise ValueError(f"repository {section!r} must explicitly enable package signature verification")
        for option_name in signature_options:
            if not _parse_boolean(options[option_name], f"{section}.{option_name}"):
                raise ValueError(f"repository {section!r} must keep {option_name} enabled")

    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8"), tuple(sections)


def _validated_repository_configuration(payload: bytes) -> bytes:
    """Compatibility seam used by tests and the add operation."""
    return _parse_repository_configuration(payload)[0]


def _read_bounded_regular_file(directory_descriptor: int, filename: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise ValueError(f"existing repository entry {filename!r} is not a regular file")
        if file_status.st_uid != 0:
            raise ValueError(f"existing repository file {filename!r} is not owned by root")
        if file_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"existing repository file {filename!r} is writable by a non-root group or user")
        if file_status.st_size > _MAX_EXISTING_REPOSITORY_FILE_BYTES:
            raise ValueError(f"existing repository file {filename!r} exceeds 1 MiB")
        chunks: list[bytes] = []
        remaining = _MAX_EXISTING_REPOSITORY_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _MAX_EXISTING_REPOSITORY_FILE_BYTES:
            raise ValueError(f"existing repository file {filename!r} exceeds 1 MiB")
        return content
    finally:
        os.close(descriptor)


def _repository_ids_from_existing_file(payload: bytes, filename: str) -> set[str]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"existing repository file {filename!r} is not valid UTF-8") from exc
    parser = configparser.RawConfigParser(interpolation=None, strict=False, empty_lines_in_values=False)
    try:
        parser.read_string(text)
    except (configparser.Error, ValueError) as exc:
        raise ValueError(f"could not parse existing repository file {filename!r}: {exc}") from exc
    return {section.casefold() for section in parser.sections() if section.casefold() != "main"}


def _existing_repository_ids(directory_descriptor: int) -> set[str]:
    repository_ids: set[str] = set()
    for filename in sorted(os.listdir(directory_descriptor)):
        if not filename.casefold().endswith(".repo"):
            continue
        payload = _read_bounded_regular_file(directory_descriptor, filename)
        repository_ids.update(_repository_ids_from_existing_file(payload, filename))
    return repository_ids


def _find_duplicate_repository_ids(new_repository_ids: tuple[str, ...], primary_descriptor: int) -> set[str]:
    existing_ids = _existing_repository_ids(primary_descriptor)
    for directory in _KNOWN_REPOSITORY_DIRECTORIES:
        if directory == _ADD_DIRECTORY:
            continue
        descriptor: int | None = None
        try:
            descriptor = _open_trusted_directory(directory)
        except FileNotFoundError:
            continue
        try:
            existing_ids.update(_existing_repository_ids(descriptor))
        finally:
            if descriptor is not None:
                os.close(descriptor)
    new_ids = {repository_id.casefold() for repository_id in new_repository_ids}
    return new_ids.intersection(existing_ids)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write while creating repository configuration")
        offset += written


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

        # unlink with dir_fd removes the directory entry validated above. It
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


def add_repository_file(filename: str, stream=None) -> int:
    """Atomically create one new validated repository configuration file."""
    if os.geteuid() != 0:
        return _fail("must be run as root", 1)
    try:
        destination = _validate_add_filename(filename)
        input_stream = stream if stream is not None else sys.stdin.buffer
        payload, repository_ids = _parse_repository_configuration(
            input_stream.read(_MAX_CONFIGURATION_BYTES + 1)
        )
    except (AttributeError, OSError, ValueError) as exc:
        return _fail(f"invalid repository configuration: {exc}")

    directory_descriptor: int | None = None
    temporary_descriptor: int | None = None
    temporary_name = f".{destination}.{secrets.token_hex(16)}.tmp"
    temporary_exists = False
    installed = False
    try:
        directory_descriptor = _open_trusted_directory(_ADD_DIRECTORY)
        duplicate_ids = _find_duplicate_repository_ids(repository_ids, directory_descriptor)
        if duplicate_ids:
            joined_ids = ", ".join(sorted(duplicate_ids))
            return _fail(f"repository ID already exists; refusing to shadow it: {joined_ids}", 1)

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temporary_descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_descriptor)
        temporary_exists = True
        os.fchown(temporary_descriptor, 0, 0)
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        # Keep the temporary content private until all bytes are durable; make
        # the installed configuration conventionally readable only immediately
        # before the atomic link into the repository directory.
        os.fchmod(temporary_descriptor, 0o644)
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None

        # linkat creates the destination atomically and fails rather than
        # replacing an existing .repo file. The temporary name stays inside
        # the opened trusted directory throughout the operation.
        os.link(
            temporary_name,
            destination,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        installed = True
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_exists = False
        os.fsync(directory_descriptor)
    except FileExistsError:
        return _fail("a repository configuration for this address already exists; refusing to overwrite it", 1)
    except (OSError, PermissionError, ValueError) as exc:
        return _fail(f"could not create repository configuration: {exc}", 1)
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_exists and directory_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        if directory_descriptor is not None:
            os.close(directory_descriptor)

    if not installed:
        return _fail("repository configuration was not installed", 1)
    print(f"Installed repository configuration: {_ADD_DIRECTORY}/{destination}")
    return 0


def _handle_termination(_signum, _frame) -> None:  # type: ignore[no-untyped-def]
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _handle_termination)
    parser = argparse.ArgumentParser(
        prog="dendro-repo-helper",
        description="Narrow privileged helper for adding or deleting one Dendro repository configuration file.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    delete = subcommands.add_parser("delete", help="delete one validated .repo file")
    delete.add_argument("repository_file", metavar="FILE")
    add = subcommands.add_parser("add", help="install one validated .repo file from standard input")
    add.add_argument("destination_filename", metavar="FILENAME")
    arguments = parser.parse_args(argv)

    if arguments.command == "delete":
        return delete_repository_file(arguments.repository_file)
    if arguments.command == "add":
        return add_repository_file(arguments.destination_filename)
    return _fail("unsupported operation")


if __name__ == "__main__":
    raise SystemExit(main())