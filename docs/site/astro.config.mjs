// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

const BASE = '/kube-agents';
const FEEDBACK_FORM_URL =
  'https://docs.google.com/forms/d/e/1FAIpQLSfw5eGttWOrii7bvSUmALmRbqpxDRWKmdHoImEZZNe6hOtVtQ/viewform';

// Base URL matches the default GitHub Pages path
// (https://<owner>.github.io/kube-agents/) so relative links resolve
// identically in dev and in prod. Overridable via `--base` in a
// future deploy workflow if the site is served from a custom domain.
export default defineConfig({
  site: 'https://gke-labs.github.io',
  base: BASE,
  // Short link for people who cannot open an issue on GitHub (see the
  // contributing page's "Where to file issues"). Astro emits a static
  // meta-refresh page at <base>/feedback/ pointing at the form.
  redirects: {
    '/feedback': FEEDBACK_FORM_URL,
  },
  integrations: [
    starlight({
      title: 'kube-agents',
      description:
        'An autonomous agentic harness for Kubernetes. Proactive fleet audits, declarative GitOps remediation, and ChatOps in one place.',
      logo: undefined,
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/gke-labs/kube-agents',
        },
      ],
      editLink: {
        baseUrl:
          'https://github.com/gke-labs/kube-agents/edit/main/docs/site/',
      },
      // Pin dark theme before first paint. Belt-and-braces with the
      // theme.css overrides that already apply under both
      // [data-theme='light'] and [data-theme='dark'].
      head: [
        {
          tag: 'script',
          attrs: { 'is:inline': true },
          content: "document.documentElement.dataset.theme = 'dark';",
        },
        // Client-side Mermaid renderer. Upgrades ```mermaid code blocks
        // to SVG diagrams and tucks the source in a <details> block.
        // See public/mermaid-render.js.
        {
          tag: 'script',
          attrs: {
            type: 'module',
            src: `${BASE}/mermaid-render.js`,
          },
        },
      ],
      // Palette + typography live in one file so the whole visual
      // system is swappable.
      customCss: ['./src/styles/theme.css'],
      // Night Owl is the closest bundled syntax theme to the sampled
      // terminal palette (navy ground; cyan / violet / coral / amber
      // tokens). Surfaces are pinned to this site's own navy so a code
      // block reads as an inset buffer rather than a foreign panel.
      expressiveCode: {
        themes: ['night-owl'],
        styleOverrides: {
          codeBackground: '#0b1322',
          borderColor: '#2b3650',
          // Expressive Code already gives an overflowing block a
          // permanent scrollbar rather than an overlay one; these only
          // move it off Night Owl's blue (#084d8180) onto the site's
          // teal, so it reads as part of the block's frame. The table
          // rule in theme.css matches these two values.
          scrollbarThumbColor: '#2090af80',
          scrollbarThumbHoverColor: '#2acacacc',
          frames: {
            editorTabBarBackground: '#0a1120',
            editorActiveTabBackground: '#0b1322',
            terminalBackground: '#0b1322',
            terminalTitlebarBackground: '#0a1120',
          },
        },
      },
      // Empty component override drops the theme toggle from the
      // navbar: the palette is a single dark terminal scheme, so
      // there is no second theme to switch to.
      components: {
        ThemeSelect: './src/components/ThemeSelect.astro',
        ThemeProvider: './src/components/ThemeProvider.astro',
        Hero: './src/components/Hero.astro',
      },
      sidebar: [
        {
          label: 'Overview',
          items: [
            { label: 'Introduction', link: '/' },
            { label: 'What is kube-agents', link: '/overview/what-is-kube-agents/' },
            { label: 'Proactive autonomy', link: '/overview/proactive-autonomy/' },
            { label: 'Architecture', link: '/overview/architecture/' },
          ],
        },
        {
          label: 'Install',
          items: [
            { label: 'Prerequisites', link: '/install/prerequisites/' },
            { label: 'Quick start (GKE)', link: '/install/quickstart-gke/' },
            { label: 'Manual install', link: '/install/manual/' },
            { label: 'Helm and Kind', link: '/install/helm-and-kind/' },
            { label: 'Uninstall', link: '/install/uninstall/' },
          ],
        },
        {
          label: 'Concepts',
          items: [{ autogenerate: { directory: 'concepts' } }],
        },
        {
          label: 'Skill catalog',
          link: '/skills/',
        },
        {
          label: 'Operator',
          items: [{ autogenerate: { directory: 'operator' } }],
        },
        {
          label: 'Deploy',
          items: [{ autogenerate: { directory: 'deploy' } }],
        },
        {
          label: 'Reference',
          items: [{ autogenerate: { directory: 'reference' } }],
        },
        {
          label: 'Contributing',
          link: '/contributing/',
        },
      ],
    }),
  ],
});
