#!/usr/bin/env bash
# Provide a current AppStream validator without changing the Jammy AppImage
# build and smoke-test environment. The wrapper is placed first on PATH for the
# subsequent build step and validates only read-only workspace content.
set -euo pipefail

readonly VALIDATOR_IMAGE='dendro-appstream-validator:1.0.2'
readonly UBUNTU_NOBLE_AMD64_IMAGE='ubuntu@sha256:a2d43bffc46670d7108bb3c0468c67c5557c4c92e83e1e34cbff1322ad1b94dc'
readonly APPSTREAM_PACKAGE_VERSION='1.0.2-1build6'
readonly VALIDATOR_BIN_DIR="${RUNNER_TEMP:?RUNNER_TEMP must be set}/dendro-appstream-validator-bin"
readonly VALIDATOR_WRAPPER="${VALIDATOR_BIN_DIR}/appstreamcli"

if [[ -z "${GITHUB_WORKSPACE:-}" ]]; then
    printf 'GITHUB_WORKSPACE must be set by GitHub Actions.\n' >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    printf 'Docker is required to run the pinned AppStream validator container.\n' >&2
    exit 1
fi

# Noble supplies AppStream 1.0, which understands current upstream metadata
# such as <developer> and the vcs-browser URL type. The image digest and
# package version make this validator selection explicit and reviewable.
docker build --pull --tag "$VALIDATOR_IMAGE" --file - "$RUNNER_TEMP" <<DOCKERFILE
FROM ${UBUNTU_NOBLE_AMD64_IMAGE}
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --yes \
        appstream=${APPSTREAM_PACKAGE_VERSION} \
    && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["appstreamcli"]
DOCKERFILE

mkdir -p "$VALIDATOR_BIN_DIR"
cat > "$VALIDATOR_WRAPPER" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_WORKSPACE:?GITHUB_WORKSPACE must be set by GitHub Actions}"
exec docker run --rm \
    --network none \
    --volume "${GITHUB_WORKSPACE}:/workspace:ro" \
    --workdir /workspace \
    dendro-appstream-validator:1.0.2 "$@"
WRAPPER
chmod 0755 "$VALIDATOR_WRAPPER"

version_output=$("$VALIDATOR_WRAPPER" --version)
if [[ "$version_output" != *'AppStream version: 1.0.2'* ]]; then
    printf 'Pinned validator did not report AppStream 1.0.2:\n%s\n' "$version_output" >&2
    exit 1
fi

"$VALIDATOR_WRAPPER" validate --no-net data/io.github.xyasharx.Dendro.metainfo.xml
printf 'Prepared AppStream 1.0.2 validator at %s\n' "$VALIDATOR_WRAPPER"
