"""Pure-Python tests for RPM EVR comparison, package specs, and taxonomy states."""

from __future__ import annotations

import pytest

from dendro.core.package_types import (
    PackageInfo,
    PackageState,
    compare_rpm_evr,
    rpm_vercmp,
)
from dendro.core.taxonomy import category_matches


@pytest.mark.parametrize(
    ("lhs", "rhs", "expected"),
    [
        ("1.0", "1.0", 0),
        ("1.0", "1.1", -1),
        ("1.10", "1.2", 1),
        ("001", "1", 0),
        ("1.0~rc1", "1.0", -1),
        ("1.0~rc1", "1.0~rc2", -1),
        ("1.0^git1", "1.0", 1),
        ("1.0^git1", "1.0.1", -1),
        ("1.0", "1.0a", -1),
        ("1.2", "1.a", 1),
    ],
)
def test_rpm_vercmp_matches_librpm_semantics(lhs: str, rhs: str, expected: int) -> None:
    assert rpm_vercmp(lhs, rhs) == expected
    assert rpm_vercmp(rhs, lhs) == -expected


def test_compare_rpm_evr_orders_epochs_versions_and_releases() -> None:
    assert compare_rpm_evr(("1", "1.0", "1.fc44"), ("0", "9.9", "1.fc44")) == 1
    assert compare_rpm_evr(("", "2.0", "1.fc44"), ("0", "2.0", "1.fc44")) == 0
    assert compare_rpm_evr(("(none)", "2.0", "1.fc44"), ("0", "2.0", "2.fc44")) == -1
    assert (
        compare_rpm_evr(("0", "24.2.5", "1.fc44"), ("0", "24.3.0~rc1", "1.fc44")) == -1
    )


def test_package_info_transaction_spec_respects_action_precision() -> None:
    reinstall_pkg = PackageInfo(
        name="bash",
        version="5.2.37",
        release="1.fc44",
        arch="x86_64",
        epoch="0",
        state=PackageState.QUEUED_REINSTALL,
    )
    assert reinstall_pkg.transaction_spec == "bash-5.2.37-1.fc44.x86_64"

    downgrade_pkg = PackageInfo(
        name="mesa-libGL",
        version="24.3.0",
        release="2.fc44",
        arch="x86_64",
        state=PackageState.QUEUED_DOWNGRADE,
        queued_target_spec="mesa-libGL-24.2.5-1.fc44.x86_64",
        queued_target_version="24.2.5-1.fc44",
    )
    assert downgrade_pkg.transaction_spec == "mesa-libGL-24.2.5-1.fc44.x86_64"

    sync_pkg = PackageInfo(
        name="nano",
        version="8.2",
        release="1.fc44",
        arch="x86_64",
        state=PackageState.QUEUED_DISTRO_SYNC,
        queued_target_spec="nano.x86_64",
        queued_target_version="8.1-1.fc44",
    )
    assert sync_pkg.transaction_spec == "nano.x86_64"


@pytest.mark.parametrize(
    "state",
    [
        PackageState.QUEUED_REINSTALL,
        PackageState.QUEUED_DOWNGRADE,
        PackageState.QUEUED_DISTRO_SYNC,
    ],
)
def test_queued_taxonomy_matches_reinstall_downgrade_and_sync(
    state: PackageState,
) -> None:
    pkg = PackageInfo(name="pkg", version="1", release="1", arch="x86_64", state=state)
    assert category_matches(pkg, "queued") is True
