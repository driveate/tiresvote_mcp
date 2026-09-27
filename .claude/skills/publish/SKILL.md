---
name: publish
description: Publish a tiresvote-mcp release to PyPI — version sync, preflight, build, upload, verification. Use when the user asks to release or publish this package ("publish", "release", "new version").
---

# Publish tiresvote-mcp to PyPI

Release process for the package `tiresvote-mcp` (normalized artifact name
`tiresvote_mcp`), repository `https://github.com/driveate/tiresvote_mcp.git`,
default branch `main`. The version lives in three places that must stay
identical: `version` in `pyproject.toml`, `__version__` in
`src/tiresvote_mcp/__init__.py`, and the project's own entry in `uv.lock`.

References: <https://docs.astral.sh/uv/guides/package/> and
<https://docs.pypi.org/api/json/>.

## Scope and authorization

- Run this workflow only on an explicit release request — that request is
  the authorization; do not re-ask. A request to prepare or dry-run a
  release stops before the upload step. Requests to create this skill,
  commit or push are unrelated work: perform them and do not enter this
  workflow.
- A missing publishing credential is a setup blocker for the upload step
  only, not renewed permission: finish local validation and preparation,
  report the exact state, and ask the user to configure credentials through
  their secret store — never ask for a token in chat.
- When resuming a prepared or partially uploaded release, go directly to
  **Failure recovery** before selecting a version or running new-release
  preflight.

## Credentials

- `UV_PUBLISH_TOKEN` from the process environment is preferred. An ignored
  `.env` may supply it via `uv run --env-file .env --no-sync ...` — never
  `source` shell code, never print, log or persist the token. If env auth
  already works, `.env` is not needed.
- `WHEELSIZE_API_KEY` is the live-API credential — never use it for
  publishing. No live Tires API tests belong to a release; they are
  separately opt-in.
- First-upload caveat: a project-scoped PyPI token cannot exist before the
  project does, so the first upload may need an account-level token or
  configured trusted publishing (`--trusted-publishing always` on a
  prepared publisher). Once the project exists, prefer a project-scoped
  token.

## Version selection

- An explicit version from the user wins as given.
- `patch` / `minor` / `major` bumps from `version` in `pyproject.toml`.
- Unspecified: if the `v<current>` tag exists, inspect
  `git log v<current>..HEAD --oneline`; if it does not, review the
  release-relevant history directly. `feat:` commits → minor, fixes/chores
  only → patch, breaking (`!`) → major. State the chosen version in the
  report.
- Check PyPI state before deciding, distinguishing HTTP status from
  network/auth failures — never treat any error as "unpublished":

  ```sh
  curl -s -o /dev/null -w '%{http_code}\n' \
    https://pypi.org/pypi/tiresvote-mcp/json
  ```

  `404` means unpublished, `200` means the package already has releases.
- First release (404, no prior versions): the current `0.1.0` may be
  reused as-is. If the owner explicitly chose another version, or releases
  already exist, sync the files to the chosen version instead.

## Steps

Local validation and preparation — no publishing credential needed:

1. **New-release preflight** — abort and report on any failure:
   - `git fetch origin`; on `main`, clean tree, in sync with `origin/main`.
   - `uv run ruff check .` and `uv run pytest -m "not integration" -q`
     green (offline suite only).
   - `git var GIT_AUTHOR_IDENT` and `git var GIT_COMMITTER_IDENT` show the
     configured `users.noreply.github.com` email — the privacy convention of this
     repository/account; preserve it, never mutate global git config.
   - `git rev-parse -q --verify refs/tags/vX.Y.Z` must fail — the tag is
     unused. If it already exists from a previous attempt, verify it points
     at the release commit and reuse it; never move or replace a tag.
2. **Version sync** — set `X.Y.Z` in `pyproject.toml` and
   `src/tiresvote_mcp/__init__.py`, then `uv lock`.
3. **Release commit and tag** — `git add pyproject.toml
   src/tiresvote_mcp/__init__.py uv.lock`, then commit only when something
   is actually staged (`git diff --cached --quiet` tells): staged →
   `git commit -m "chore: release X.Y.Z"`; nothing staged (reused version)
   → the reviewed current HEAD is the release commit. Then `git tag
   vX.Y.Z` unless the correct tag already exists. Do NOT push: a pushed
   tag for an unpublished version must not exist.
4. **Build** into a fresh version-specific directory — `uv build --out-dir
   dist/vX.Y.Z` expects `tiresvote_mcp-X.Y.Z.tar.gz` +
   `tiresvote_mcp-X.Y.Z-py3-none-any.whl`. Never `rm -rf dist`. On a resumed
   or partial release where identical artifacts were already built, keep
   and reuse them — do not rebuild over them. Record the release commit SHA,
   artifact filenames and SHA256 hashes in an ignored local release record
   before upload so a later retry can verify their provenance.
5. **Wheel check from outside the checkout** — install the built wheel into
   a throwaway venv, then run with absolute paths on both sides:

   ```sh
   /path/to/venv/bin/python /absolute/path/to/tiresvote_mcp/scripts/check_stdio.py \
     -- /path/to/venv/bin/tiresvote-mcp
   ```

   (stdio handshake: tools/prompts/resource surface, JSON-RPC stdout only).
6. **Upload dry-run** on the exact artifacts — exercises the request path
   only; it does NOT prove credentials or server acceptance:

   ```sh
   uv publish --dry-run \
     dist/vX.Y.Z/tiresvote_mcp-X.Y.Z.tar.gz \
     dist/vX.Y.Z/tiresvote_mcp-X.Y.Z-py3-none-any.whl
   ```

**Authentication gate** — `UV_PUBLISH_TOKEN` or trusted publishing must be
resolvable per Credentials; if not, stop at the prepared state and report
the blocker.

7. **Upload** the exact artifacts (explicit paths, never a glob or the
   `dist/*` default):

   ```sh
   uv publish \
     dist/vX.Y.Z/tiresvote_mcp-X.Y.Z.tar.gz \
     dist/vX.Y.Z/tiresvote_mcp-X.Y.Z-py3-none-any.whl
   ```

   uv skips files already uploaded with identical content; adding
   `--check-url https://pypi.org/simple/` makes that check explicit.
8. **Verify the exact version before pushing**:
   - Fetch `https://pypi.org/pypi/tiresvote-mcp/X.Y.Z/json` requiring HTTP
     200 (distinguish 404 from network/auth failures). Require both expected
     filenames in `urls[]` and compare each `digests.sha256` with the local
     artifact's SHA256; a release containing only one file is incomplete.
   - `uvx --refresh --isolated --from 'tiresvote-mcp==X.Y.Z' tiresvote-mcp
     --version` and `--help` — the wheel installs from PyPI and prints CLI
     usage (stdio is the only transport).
9. **Push** only after a verified upload: `git push --atomic origin main
   vX.Y.Z` — atomic keeps `main` and the tag from landing partially where
   supported; otherwise report the exact partial refs state.
10. **Report** version, PyPI link, verification results. Any public
    README/status change that mentions the published release is a separate
    commit/push AFTER publication — never folded into the immutable release
    commit or tag.

## Failure recovery

- Resume the existing version: `git fetch origin`, require `main` and a
  clean tree, then identify the release commit, any existing `vX.Y.Z` tag,
  and retained artifacts. Check the three version sources at that commit,
  the tag target, and the artifacts' recorded build provenance and hashes.
  A local release commit ahead of `origin/main` is expected; synchronization
  is a new-release requirement, not a recovery prerequisite. If provenance
  cannot be established, stop and report the missing evidence.
- Inspect the exact-version PyPI JSON (404 means no upload; other failures
  are inconclusive), then continue from the first incomplete step. Keep the
  selected version, commit, tag and existing artifacts: do not repeat
  version selection, `uv lock`, or a completed build. If preparation stopped
  before tagging or building, perform only the missing steps for the
  verified release commit.
- Ambiguous or partial upload: fetch the version JSON, compare `urls[]`
  filenames and `digests.sha256` against local artifacts, retry only
  missing identical files. A file already published with a different
  digest cannot be replaced — choose a new version.
- Upload succeeded but `git push` failed (e.g. remote moved): keep the
  commit, tag and artifacts immutable; `git fetch`, integrate new remote
  history without rewriting the tagged release commit, retry the push —
  never rebase a published tagged commit onto new code under the same
  version. Before pushing, `git merge-base --is-ancestor vX.Y.Z main`
  must succeed so the original release commit remains reachable from `main`.
- Never `git reset --hard`, never force-push or replace a tag, never delete
  or yank a published release, never blindly retry an ambiguous upload.
- Report the truthful partial state (committed/tagged/uploaded, what
  remains). A failed optional step does not make the PyPI upload failed.

## MCP Registry (optional, separate step)

No `server.json` or registry registration exists in this repository — do
not claim a namespace or copy registry facts from other projects. Only when
a deliberately configured `server.json` with verified ownership exists AND
the user asked for registry publication: run it after the PyPI upload
succeeds and report it as a separate result from the PyPI outcome.
