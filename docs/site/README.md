# kube-agents docs site

Astro + [Starlight](https://starlight.astro.build/) source for the `kube-agents` documentation site.

## Local preview

```bash
cd docs/site
npm install
npm run dev
```

Open <http://localhost:4321/kube-agents/> in a browser. Edits to `src/content/docs/**/*.{md,mdx}` hot-reload.

## Build

```bash
npm run build         # emits static site to dist/
npm run preview       # serve dist/ locally on port 4321
```

## Layout

```text
docs/site/
├── astro.config.mjs         # base URL, sidebar, theme wiring
├── package.json
├── src/
│   ├── components/          # Hero, ThemeSelect, ThemeProvider overrides (light-only)
│   ├── content/
│   │   ├── docs/            # every page in the site
│   │   └── content.config.ts
│   ├── plugins/             # remark plugins
│   └── styles/theme.css     # palette + typography
└── public/                  # static assets
```

## Adding a page

1. Create a `.md` or `.mdx` file under `src/content/docs/<section>/`.
2. Add frontmatter with at least `title:`. If the section auto-generates its sidebar (see `astro.config.mjs`), the page appears automatically.
3. Verify locally with `npm run dev`.

## CI

Two GitHub Actions workflows:

- **[`.github/workflows/docs-build.yml`](../../.github/workflows/docs-build.yml)** — runs on every PR that touches `docs/site/**`. Builds the site to catch broken links / bad frontmatter. Doesn't publish.
- **[`.github/workflows/docs-deploy.yml`](../../.github/workflows/docs-deploy.yml)** — publishes to GitHub Pages on push to `main` (and via `workflow_dispatch`). See below.

## Publish from your fork

The deploy workflow will happily run on your fork, once you tell GitHub to accept a Pages build from Actions:

1. **Enable Pages.** In your fork's repo settings → Pages, set **Source** to **GitHub Actions**.
2. **Trigger the workflow.** Go to the **Actions** tab, pick **Docs Deploy**, click **Run workflow** on the `main` branch. Or just push a commit that touches `docs/site/**` — the workflow watches that path.
3. **Visit the site** at `https://<your-github-username>.github.io/kube-agents/` after the deploy completes (usually under 2 minutes).

The `astro.config.mjs` sets `base: '/kube-agents'`, matching the default Pages URL when the repo is named `kube-agents`. If you renamed the fork, edit `base` in `astro.config.mjs` to match the new URL segment.

## Publish from upstream

`gke-labs/kube-agents` doesn't have Pages enabled yet, so `docs-deploy.yml` is inert on the upstream repo. To enable, a maintainer needs to do the three-step Pages setup above on the upstream repo — no workflow changes required.

## Prettier

The repo enforces prettier on Markdown, YAML, and JSON. Run before committing:

```bash
npx prettier --write docs/site/src/content/docs/**/*.{md,mdx}
```

The `Prettier Check` workflow validates PRs.
