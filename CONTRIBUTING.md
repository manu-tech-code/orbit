# Branches and releases

`main` holds released code. Work on a branch and open a pull request into it.

```
feat/… fix/… chore/…  ──PR──▶  main  ──▶  release/<version> + tag v<version>
```

1. **Start a branch**: `git switch main && git pull && git switch -c feat/floor-plan-walls`.
   Prefixes: `feat/`, `fix/`, `bug/`, `chore/`, `docs/`, `refactor/`, `perf/`, `test/`, `ci/`.
2. **Open a pull request into `main`.** The *Python checks* workflow runs the five test scripts
   in `scripts/`, which check the devices, tripwire, router alerts, floor plan and other routers (MikroTik, Grandstream) without
   touching the network. Keep them passing.
3. **Release**: bump `__version__` in `orbit/__init__.py` (the only place the version is
   written; `mac/build.sh` stamps it into the app) and take it in through a pull request.
4. **The Release workflow** then creates `release/<version>`, the annotated tag `v<version>`
   and a *draft* GitHub release with notes generated from the merged pull requests.
5. **Build the artifacts and publish** from a Mac (published releases are immutable, so the
   assets have to go on while it's a draft):

   ```sh
   git fetch && git switch release/<version>
   scripts/release.sh --upload
   ```

   That attaches `orbit-<version>.tar.gz`, `Orbit-<version>.zip` and `checksums.txt`
   to the draft and publishes it. `scripts/install.sh` installs whatever the latest
   release is, so what people get is always what was published.

The Mac app is signed ad-hoc (`codesign -s -`). It runs on the Mac that built it; sharing
it with someone else needs a Developer ID certificate and notarization, so the installer
checks the tarball's SHA-256 rather than the app's signature.
