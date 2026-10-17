"""Tests for the isolated modern AppStream validator setup script."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "ci" / "prepare_appstream_validator.sh"


def _write_fake_docker(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [[ "$1" == "build" ]]; then
    cat > "$DOCKERFILE_LOG"
    exit 0
fi
if [[ "$1" == "run" ]]; then
    last="${!#}"
    if [[ "$last" == "--version" ]]; then
        printf '%s\\n' 'AppStream version: 1.0.2'
    fi
    exit 0
fi
printf 'Unexpected docker invocation: %s\\n' "$*" >&2
exit 64
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_prepare_appstream_validator_installs_a_read_only_wrapper(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    metadata = workspace / "data" / "io.github.xyasharx.Dendro.metainfo.xml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("<component type='desktop-application'/>\n", encoding="utf-8")
    appdir = workspace / "AppDir"
    appdir.mkdir()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin / "docker")

    docker_log = tmp_path / "docker.log"
    dockerfile_log = tmp_path / "Dockerfile"
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()

    environment = os.environ.copy()
    environment.update(
        {
            "DOCKER_LOG": str(docker_log),
            "DOCKERFILE_LOG": str(dockerfile_log),
            "GITHUB_WORKSPACE": str(workspace),
            "RUNNER_TEMP": str(runner_temp),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=workspace,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    wrapper = runner_temp / "dendro-appstream-validator-bin" / "appstreamcli"
    assert wrapper.is_file()

    dockerfile = dockerfile_log.read_text(encoding="utf-8")
    assert "FROM ubuntu@sha256:a2d43bffc46670d7108bb3c0468c67c5557c4c92e83e1e34cbff1322ad1b94dc" in dockerfile
    assert "appstream=1.0.2-1build6" in dockerfile

    wrapper_source = wrapper.read_text(encoding="utf-8")
    assert "--network none" in wrapper_source
    assert '"${GITHUB_WORKSPACE}:${GITHUB_WORKSPACE}:ro"' in wrapper_source
    assert '--workdir "${GITHUB_WORKSPACE}"' in wrapper_source

    validation = subprocess.run(
        [str(wrapper), "validate", "--no-net", "data/io.github.xyasharx.Dendro.metainfo.xml"],
        cwd=workspace,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert validation.returncode == 0, validation.stderr

    tree_validation = subprocess.run(
        [str(wrapper), "validate-tree", str(appdir)],
        cwd=workspace,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert tree_validation.returncode == 0, tree_validation.stderr
    invocations = docker_log.read_text(encoding="utf-8")
    assert f"build --pull --tag dendro-appstream-validator:1.0.2 --file - {runner_temp}" in invocations
    assert "run --rm --network none" in invocations
    assert f"--volume {workspace}:{workspace}:ro --workdir {workspace}" in invocations
    assert "dendro-appstream-validator:1.0.2 validate --no-net data/io.github.xyasharx.Dendro.metainfo.xml" in invocations
    assert f"dendro-appstream-validator:1.0.2 validate-tree {appdir}" in invocations
