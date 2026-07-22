// @ts-check
import { defineConfig } from 'astro/config';
import { unified } from '@astrojs/markdown-remark';
import starlight from '@astrojs/starlight';
import { remarkPrependBase } from './src/plugins/remark-prepend-base.mjs';

const BASE = '/kube-agents';

// Base URL matches the default GitHub Pages path
// (https://<owner>.github.io/kube-agents/) so relative links resolve
// identically in dev and in prod. Overridable via `--base` in a
// future deploy workflow if the site is served from a custom domain.
export default defineConfig({
  site: 'https://gke-labs.github.io',
  base: BASE,
  markdown: {
    processor: unified({ remarkPlugins: [remarkPrependBase(BASE)] }),
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
      // Pin light theme before first paint. Belt-and-braces with the
      // theme.css overrides that already apply under both
      // [data-theme='light'] and [data-theme='dark'].
      head: [
        {
          tag: 'script',
          attrs: { 'is:inline': true },
          content: "document.documentElement.dataset.theme = 'light';",
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
      // Empty component override drops the dark-mode toggle from the
      // navbar. Light-only site (matches core-agent).
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
