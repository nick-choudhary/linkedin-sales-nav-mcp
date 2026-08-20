# Releasing

One action releases everywhere. The repo is the single source of truth.

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
   - fails fast if the tag and the three version strings disagree,
   - runs the test suite — a broken build never publishes,
   - publishes the package to **PyPI** (trusted publishing, no tokens),
   - publishes the server to the **official MCP Registry** (GitHub OIDC, no
     secrets), syncing `server.json`'s version from the tag.

## How each platform stays in sync

| Platform | Sync mechanism | Anything to do? |
|---|---|---|
| PyPI | Release workflow publishes on tag | No |
| Official MCP Registry | Same workflow (`mcp-publisher`, GitHub OIDC) | No |
| LobeHub | Their crawler re-scans the repo on a schedule | Optional: `npx -y @lobehub/market-cli plugin update --dir .` posts the new manifest version immediately (one command, uses your saved login) |
| Glama | Re-crawls GitHub daily; ownership claimed via `glama.json` | No |
| mcp.so | Re-crawls the GitHub repo | No |
| PulseMCP | Re-crawls the GitHub repo | No |

The LobeHub CLI authenticates with a browser login, so it cannot run in CI —
that one step stays a local one-liner. Everything else is fully automatic.

## First-time setup (already done once)

- PyPI: a *pending trusted publisher* for this repo (`publish.yml`) is
  registered on the PyPI project; the first tagged release creates the
  project. No API tokens are stored anywhere.
- Glama: claimed by authenticating with GitHub after `glama.json` landed.
