# CI/CD

whatsapp-backup pairs a Python + Flask web UI with the `wacli` WhatsApp CLI. It
ships as a single Docker image built from the root [`Dockerfile`](Dockerfile) (a
multi-stage build that compiles `wacli` from Go, builds the `ui/` front-end with
Node, and serves it from a Python runtime) and is published to the GitHub
Container Registry (GHCR). This document describes the GitHub Actions workflows
that check the code, verify the image builds, and publish it.

## Workflows
| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `.github/workflows/ci.yml` | PRs + pushes to `main` | Python checks + verify the Docker image builds |
| `.github/workflows/release.yml` | Pushing a `v*` tag | Build & push the image to GHCR |

## Continuous integration (`ci.yml`)

Runs on every pull request and on every push to `main`. It has two jobs:

- **Python checks** — sets up Python 3.12, installs `requirements.txt`
  (Flask), then runs `python -m compileall app` as a fast syntax check of the
  Python sources. It also checks that the Dockerfile declares the image's
  license label, that `release.yml` passes it through, and that
  `ui/package.json` states the Unlicense.
- **Build Docker image** — builds the image with
  `docker/build-push-action@v7` using `push: false`, so every
  PR proves the multi-stage image still builds without publishing anything. The
  build context is the **repository root** (`.`) and the file is the root
  `Dockerfile`, which copies `requirements.txt`, `app/`, `ui/`, and `media/`.
  The build runs `tests/` against the compiled `wacli`, including authentication
  checks during a session write. The image exposes port `8080`.
  GitHub Actions layer caching (`type=gha`) keeps
  rebuilds fast. The image is loaded locally (`load: true`) so the job can
  check that its third-party notices exist in `/usr/share/doc/whatsapp-backup/`
  and `/app/web/`, and that its license label names every license they list
  (`scripts/licenses/check-image-notices.sh`).

### Running locally

```bash
# Python checks
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m compileall app

# Verify the Docker image builds (context = repo root, file = Dockerfile)
docker build -f Dockerfile .
```

## Releases (`release.yml`)

```
git tag v1.2.3
git push origin v1.2.3
```

The workflow first builds the image locally and runs the same notice and
license label checks as CI, then pushes it. The image is published to
`ghcr.io/l-k-m/whatsapp-backup` with tags `1.2.3`,
`1.2`, and `latest` (the `latest` tag is skipped for pre-release tags such as
`v1.2.3-rc.1`). GHCR lowercases the namespace, so even though the owner is
`L-K-M` the image path is `ghcr.io/l-k-m/whatsapp-backup`. Pull with:

```
docker pull ghcr.io/l-k-m/whatsapp-backup:1.2.3
```

> Note: the first time the image is published, the GHCR package is private.
> Make it public (or grant access) from the package settings in the repository /
> package settings if you want anonymous pulls.

## Secrets
None — GHCR push uses the built-in `GITHUB_TOKEN`.
