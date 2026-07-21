import { visit } from 'unist-util-visit';

/**
 * Prepend Astro's `base` to any Markdown link starting with `/`.
 * Keeps content files decoupled from the deploy base URL: authors
 * write `[text](/concepts/tools/)`, at build time the plugin turns
 * it into `[text](/core-agent/concepts/tools/)` (or whatever `base`
 * resolves to). Change the base in astro.config.mjs and content
 * doesn't move.
 *
 * Skipped:
 *   - external URLs (any scheme://)
 *   - protocol-relative (//foo)
 *   - anchor-only links (#foo)
 *   - links already prefixed with the base
 */
export function remarkPrependBase(base) {
  const normalizedBase = base.replace(/\/$/, '');
  if (!normalizedBase) {
    return () => (tree) => tree;
  }
  return () => (tree) => {
    // `link` covers inline links `[text](/foo)`.
    // `definition` covers the destination of reference-style links —
    // `[ref]: /foo` — since `linkReference` nodes only hold the ref
    // identifier, not the URL itself.
    visit(tree, ['link', 'definition'], (node) => {
      const url = node.url;
      if (typeof url !== 'string' || url === '') return;
      if (/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) return;
      if (url.startsWith('//')) return;
      if (url.startsWith('#')) return;
      if (!url.startsWith('/')) return;
      if (url === normalizedBase || url.startsWith(normalizedBase + '/')) return;
      node.url = normalizedBase + url;
    });
  };
}
