# CI and Release

> **Audience:** Internal Qvest engineers.<br>
> **Companion docs:** [architecture.md](architecture.md) for component deep-dives (including the go-mxl lock-step constraint); [operations.md](operations.md) for how the manifests artifact lands on the cluster; [README.md](../README.md) for the 60-second story.

---

## 1. Overview

There are two independent build outputs:

1. **Manifests OCI artifact** — the rendered `k8s/` tree, pushed to `ghcr.io/qvest-digital/mxl-dmf-demo-app-manifests` on every qualifying push. This is what Flux reconciles onto the cluster. Produced by `.github/workflows/build.yml`.

2. **Compositor image** — a C++/GStreamer container built from `compositor/Dockerfile.mxlk8s`, pushed to `ghcr.io/qvest-digital/mxl-dmf-demo-app/compositor-mxlk8s`. Produced by `.github/workflows/build-compositor.yml`. The compositor image is versioned separately from the manifests artifact and must stay in lock-step with the mxl-k8s gateway's `go-mxl` tag (see §4 and [architecture.md §8 — go-mxl lock-step](architecture.md#go-mxl-lock-step)).

Versioning is handled by **release-please** using conventional commits, producing a `1.0.0-rc.N` pre-release series. Merging the release PR that release-please opens cuts a `vN` tag, updates `CHANGELOG.md`, and publishes a GitHub release; the `v*` tag triggers the manifests build (`build.yml`) and the published release triggers the compositor build (`build-compositor.yml`).

---

## 2. The two workflows

### `build.yml` — Push manifests

**Triggers:**

| Event | Condition |
|---|---|
| `push` | Branches: `main`; tags: `v*` |
| `pull_request` | Against `main`; paths: `k8s/**` or `.github/workflows/build.yml` |
| `workflow_dispatch` | Manual; accepts optional `pr_env` and `ref` inputs |
| `workflow_call` | Called by `mxl-dmf-terraform`'s `cd-pr-feature-up.yml`; same `pr_env`/`ref` inputs |

**What it does:** Copies the entire `k8s/` tree (manifests plus the `config/` subdirectory) to a staging path, then runs `flux push artifact` to push it as an OCI artifact tagged with the short commit SHA. A second `flux tag artifact` call then applies a human-readable tag based on the trigger:

| Trigger | Tag applied |
|---|---|
| Push to `main` | `latest` |
| Pull request | `pr-<N>` (e.g. `pr-42`) |
| Version tag (`v*`) | The tag name (e.g. `v1.0.0-rc.3`) |
| `workflow_call` / `workflow_dispatch` with `pr_env` input | `<pr_env>-latest` (e.g. `pr-185-latest`) |

The `config/` subdirectory must be included in the copy — a flat `*.yaml` glob would silently drop it, causing kustomize to fail in Flux with a missing-file error.

For how Flux reconciles the artifact onto the cluster, see [operations.md §1 — How deployment works](operations.md#1-how-deployment-works).

### `build-compositor.yml` — Build and push compositor image

**Triggers:**

| Event | Condition |
|---|---|
| `push` | Branch: `main`; paths: `compositor/**` or `.github/workflows/build-compositor.yml` |
| `pull_request` | Against `main`; same paths |
| `release` | Type: `published` (i.e. when release-please cuts a new tag and publishes the release) |
| `workflow_dispatch` | Manual |

**What it produces:** A Docker image pushed to `ghcr.io/qvest-digital/mxl-dmf-demo-app/compositor-mxlk8s`. Tags applied:

- Always: `<short-sha>` (7-character commit SHA)
- On `main`: `latest`
- On a release event: the semver version string without the leading `v` (e.g. `1.0.0-rc.3`), taken from `github.event.release.tag_name`. This workflow has no tag-push trigger — `build.yml` is the one that fires on `v*` tags (`github.event.release.tag_name` is reliable on `release` events where `GITHUB_REF` is not).

The image is built from `compositor/Dockerfile.mxlk8s` using `docker/build-push-action` with GitHub Actions layer caching (`scope=mxlk8s`).

**Authentication note:** The existing `ghcr-pull-secret` rendered by ExternalSecrets (used for mediamtx) covers pulls of this image too — same registry, no extra secret to wire.

---

## 3. Releases (release-please)

Release management uses [release-please](https://github.com/googleapis/release-please), configured in `.github/release-please-config.json` and `.github/release-please-manifest.json`.

### RC scheme

- **Release type:** `simple` — release-please manages a single version file.
- **Versioning:** `prerelease` with `prerelease-type: rc`, meaning each release increments the `rc.N` counter: `1.0.0-rc.0`, `1.0.0-rc.1`, `1.0.0-rc.2`, …
- **Tags:** `include-v-in-tag: true` — tags are prefixed with `v` (e.g. `v1.0.0-rc.3`); the compositor image strips the `v` for Docker convention.
- **Current version:** `1.0.0-rc.3` (from `release-please-manifest.json`).

### How it works

On every push to `main`, release-please scans conventional commits since the last tag and opens (or updates) a release PR. Merging that PR:

1. Bumps the version in `release-please-manifest.json`.
2. Updates `CHANGELOG.md` with the grouped commit list.
3. Creates a `v<N>` tag.
4. Publishes a GitHub release.

The published release event then triggers `build-compositor.yml` (`release: [published]`), and the `v*` tag triggers `build.yml` (`tags: ["v*"]`), so both build artifacts are stamped with the release version automatically.

### Changelog sections

The changelog is configured to show the following commit types as visible sections, and to hide others:

| Commit type | Section | Visible |
|---|---|---|
| `feat` | Features | yes |
| `fix` | Bug Fixes | yes |
| `deps` | Dependencies | yes |
| `perf` | Performance | yes |
| `revert` | Reverts | yes |
| `refactor` | Code Refactoring | yes |
| `build` | Build System | yes |
| `ci` | Continuous Integration | yes |
| `chore` | Miscellaneous | yes |
| `docs` | Documentation | hidden |
| `style` | Styles | hidden |
| `test` | Tests | hidden |

`docs`, `style`, and `test` commits are collected by release-please but omitted from the published changelog. `deps` commits are particularly relevant because Renovate uses that type for go-mxl base-image bumps (see §4).

See `CHANGELOG.md` for the full history.

---

## 4. Dependency automation (Renovate)

Renovate runs against this repo, configured in `renovate.json`. Two areas are managed:

### go-mxl base-image bumps

The compositor Dockerfile (`compositor/Dockerfile.mxlk8s`) pins `ghcr.io/qvest-digital/go-mxl-builder` and `ghcr.io/qvest-digital/go-mxl-runtime` via the `ARG GO_MXL_TAG` build argument. When a new `go-mxl` tag is published, Renovate opens a PR to update `ARG GO_MXL_TAG` in the Dockerfile.

These PRs are classified as `deps(compositor)` commits (configured in `renovate.json` under `packageRules`). That commit type is visible in the changelog (`deps` → "Dependencies") and is included in the next release-please RC, so the go-mxl version bump becomes part of the release record.

**Why this matters:** The `go-mxl` tag pinned in the compositor Dockerfile and the tag the mxl-k8s gateway was built from must match exactly — a mismatch causes cross-node mirror reads to fail silently or return `FLOW_INVALID`. See [architecture.md §8 — go-mxl lock-step](architecture.md#go-mxl-lock-step) for the full rationale.

### Custom regex managers

`renovate.json` registers two custom regex managers:

**1. Dockerfile `ARG GO_MXL_TAG`** (`managerFilePatterns: /^compositor/Dockerfile\.mxlk8s$/`):

Renovate matches the inline marker and the `ARG` line together:

```
# renovate: datasource=<datasource> depName=<depName>
ARG GO_MXL_TAG=<currentValue>
```

The marker is present in `compositor/Dockerfile.mxlk8s` directly above the `ARG GO_MXL_TAG` line. When a new version of `ghcr.io/qvest-digital/go-mxl-builder` is available, Renovate updates `<currentValue>` in place, which causes both build stages to use the new tag (the runtime stage reuses the same `${GO_MXL_TAG}` ARG).

**2. Inline-marker k8s image pins** (`managerFilePatterns: /^k8s/.+\.yaml$/`):

Kubernetes manifests can opt into Renovate updates by adding an inline marker on the line immediately before the `image:` field:

```yaml
# renovate: datasource=<datasource> depName=<depName> [versioning=<versioning>]
image: "<image>:<currentValue>"
```

Renovate scans matching `k8s/` YAML files for this pattern and manages those image tags directly.

### Rate limiting

`minimumReleaseAge: 3 days` — Renovate waits 3 days after a package version is published before opening a PR, reducing noise from immediately-reverted releases. `prHourlyLimit: 2` caps the number of Renovate PRs opened per hour.
