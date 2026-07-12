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
  Python sources. There is no test suite or linter config in the repo, so this
  is the lightweight gate that the code at least parses.
- **Build Docker image** — builds the image with
  `docker/build-push-action@v6` using `push: false` / `load: false`, so every
  PR proves the multi-stage image still builds without publishing anything. The
  build context is the **repository root** (`.`) and the file is the root
  `Dockerfile`, which copies `requirements.txt`, `app/`, `ui/`, and `media/`.
  The image exposes port `8080`. GitHub Actions layer caching (`type=gha`) keeps
  rebuilds fast.

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

The image is published to `ghcr.io/l-k-m/whatsapp-backup` with tags `1.2.3`,
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
