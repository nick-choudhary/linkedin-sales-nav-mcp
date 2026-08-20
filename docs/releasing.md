# Releasing

One action releases everywhere. The repo is the single source of truth.

Active platforms: **PyPI**, **official MCP Registry**, **LobeHub**, **Glama**.

## Cut a release

1. Bump the version in the three versioned files (keep them identical):
   - `pyproject.toml` → `version`
   - `sales_nav_mcp/__init__.py` → `__version__`
   - `lhm.plugin.json` → `version`
2. Commit, then tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

3. The `Publish` workflow (`.github/workflows/publish.yml`) takes over:
   - fails fast if the tag and the three version strings disagree — a skewed
     release never ships,
   - runs the test suite — a broken build never publishes,
   - builds the package and publishes it to **PyPI** (trusted publishing via
     OIDC, no stored tokens; `skip-existing` makes tag re-runs safe),
   - publishes the server to the **official MCP Registry** (`mcp-publisher`
     with GitHub OIDC, no secrets), syncing `server.json`'s version from the
     tag.

## How each platform stays in sync

| Platform | Sync mechanism | Anything to do? |
|---|---|---|
| PyPI | Release workflow publishes on tag | No |
| Official MCP Registry | Same workflow, same tag | No |
| Glama | Re-crawls the GitHub repo on its own schedule; ownership proven by `glama.json` on main | No |
| LobeHub | One command posts the new manifest to their backend | Yes — after tagging: `npx -y @lobehub/market-cli plugin update --dir .` (uses your saved browser login) |

The LobeHub CLI authenticates with an interactive browser login, so it cannot
run in CI — that one step stays a local one-liner. Everything else is fully
automatic.

Note on LobeHub's public page: the CLI updates their backend manifest
immediately, but the public listing page is refreshed by their internal
pipeline, which is currently failing platform-side (`QSTASH_TOKEN` not
configured in their deployment — reported to them). Their scheduled re-scan
picks up repo changes once that is fixed. No action needed from this repo.

## Deliberately not listed on

- **mcp.so** — charges for submission; skipped by choice.
- **PulseMCP** — not accepting new submissions.
- **Smithery** — only accepts hosted (remote URL) servers or MCPB desktop
  bundles. This server is a local stdio process driving a real logged-in
  browser by design, so their model does not fit.

If any of these change their model, the repo is already in submission shape
(README, `server.json`, live PyPI package).

## One-time setup (status)

- **PyPI**: trusted publisher configured on the project
  (`linkedin-sales-nav-mcp`, workflow `publish.yml`, no environment). The
  first tagged release created the project; v1.2.0 is live. No API tokens
  exist anywhere.
- **Official MCP Registry**: v1.2.0 published and active as
  `io.github.nick-choudhary/linkedin-sales-nav-mcp`. Ownership is proven by
  GitHub OIDC (namespace) plus the `mcp-name` comment in `README.md` (PyPI
  package). Keep both in place.
- **LobeHub**: listing claimed; `lhm.plugin.json` in this repo is the
  owner-authoritative manifest.
- **Glama**: `glama.json` is on main. Final step is a one-time click: sign
  into https://glama.ai/mcp/servers/@nick-choudhary/linkedin-sales-nav-mcp
  with the `nick-choudhary` GitHub account and press "claim this server"
  (the public page shows "Unclaimed" until this is done).
