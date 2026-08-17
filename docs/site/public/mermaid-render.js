// Client-side Mermaid rendering for kube-agents docs.
//
// Starlight's default handling shows ```mermaid blocks as syntax-highlighted
// code (via expressive-code). This script upgrades them to rendered SVG
// diagrams, adds an Expand button that pops the diagram into a full-viewport
// modal, and preserves the original source in a collapsible <details> block.
//
// Loaded via astro.config.mjs `head` config, so it runs on every page.

import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';

mermaid.initialize({
  startOnLoad: false,
  theme: 'dark',
  // `loose` is needed for htmlLabels: <br/> in labels + subgraph names
  // with parentheses. Content is authored in-repo, not user-submitted,
  // so the XSS surface is bounded to what we ship in git.
  securityLevel: 'loose',
  flowchart: { htmlLabels: true, curve: 'basis' },
  themeVariables: {
    // Match the terminal-navy site palette so diagrams don't look
    // pasted in from another site. Node fills rotate between the
    // cyan/violet tints used elsewhere for the same reason the
    // sidebar hues do — a flat diagram reads as one gray mass.
    background: '#0f1728',
    primaryColor: '#172136',
    primaryTextColor: '#e8ecf4',
    primaryBorderColor: '#2acaca',
    secondaryColor: '#272745',
    secondaryBorderColor: '#bd86f9',
    tertiaryColor: '#13303f',
    tertiaryBorderColor: '#2090af',
    lineColor: '#9aa3b8',
    textColor: '#e8ecf4',
    nodeBorder: '#2acaca',
    clusterBkg: '#131c30',
    clusterBorder: '#3f4553',
    titleColor: '#b1baf5',
    // Matches `.mermaid-rendered`'s panel fill in theme.css, so the
    // label knockouts sit flush with the surface behind them.
    edgeLabelBackground: '#172136',
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif",
  },
  // themeVariables only reaches a handful of roles, so every node in a
  // flowchart lands on primaryColor and the diagram reads as one gray
  // mass. Rotate the outline through the same six hues the sidebar
  // groups use, keyed off sibling position, and back each with a faint
  // wash of its own hue. Label text stays one bright value: colouring
  // the outline is free, colouring 11px text is not.
  //
  // `!important` is load-bearing. Mermaid emits its own rules prefixed
  // with the per-diagram id selector (`#mermaid-0 .node rect { … }`),
  // which out-specifies anything unprefixed we inject here.
  themeCSS: `
    .node rect,
    .node polygon,
    .node circle,
    .node path {
      stroke-width: 1.5px !important;
    }
    .nodes > g:nth-of-type(6n + 1) rect { stroke: #2acaca !important; fill: rgba(42, 202, 202, 0.1) !important; }
    .nodes > g:nth-of-type(6n + 2) rect { stroke: #bd86f9 !important; fill: rgba(189, 134, 249, 0.1) !important; }
    .nodes > g:nth-of-type(6n + 3) rect { stroke: #fcc140 !important; fill: rgba(252, 193, 64, 0.09) !important; }
    .nodes > g:nth-of-type(6n + 4) rect { stroke: #58c660 !important; fill: rgba(88, 198, 96, 0.1) !important; }
    .nodes > g:nth-of-type(6n + 5) rect { stroke: #fb7081 !important; fill: rgba(251, 112, 129, 0.09) !important; }
    .nodes > g:nth-of-type(6n + 6) rect { stroke: #b1baf5 !important; fill: rgba(177, 186, 245, 0.1) !important; }
    .nodeLabel,
    .nodeLabel p,
    .node .label {
      color: #f8f8f3 !important;
      fill: #f8f8f3 !important;
    }
    /* Edge captions default to a dim gray that disappears on navy. */
    .edgeLabel,
    .edgeLabel p,
    .edgeLabel .label {
      color: #ced2d6 !important;
      fill: #ced2d6 !important;
      background-color: #172136 !important;
    }
    .edgeLabel rect { fill: #172136 !important; opacity: 0.92 !important; }
    /* Subgraph frames: periwinkle title over a barely-there panel. */
    .cluster rect {
      fill: #131c30 !important;
      stroke: #3f4553 !important;
      stroke-dasharray: 4 3 !important;
    }
    /* Reach the span itself, not just the <g> wrapping it: mermaid
     * gives a subgraph title the same .nodeLabel class as a node's,
     * so an ancestor rule loses to the .nodeLabel rule above.
     * Colour only — a font-size bump here would overflow the frame,
     * whose geometry was measured before this stylesheet applied. */
    .cluster-label,
    .cluster-label p,
    .cluster-label .nodeLabel,
    .cluster-label .nodeLabel p,
    .cluster text {
      color: #b1baf5 !important;
      fill: #b1baf5 !important;
      font-weight: 600 !important;
    }
  `,
});

/**
 * Extract the mermaid source from an expressive-code <pre>. Naïve
 * textContent flattens everything onto one line because expressive-code
 * emits each source line as a separate <div class="ec-line"> with no
 * text-node newline between siblings — that breaks mermaid, which needs
 * real newlines to tokenize `subgraph`, `end`, and edge lines. Walk the
 * per-line divs and rejoin with '\n'.
 */
function extractMermaidSource(pre) {
  const lines = pre.querySelectorAll('.ec-line');
  if (lines.length) {
    return Array.from(lines)
      .map((line) => line.textContent || '')
      .join('\n')
      .trim();
  }
  return (pre.textContent || '').trim();
}

/**
 * Mermaid emits SVGs with intrinsic width/height attributes based on
 * the source diagram size, which means small diagrams stay small even
 * when their container is wide. Strip those and rely on the SVG's
 * viewBox + CSS `width: 100%` so the diagram scales to its container.
 * Keeps aspect ratio via viewBox.
 */
function makeSvgResponsive(svg) {
  svg.removeAttribute('width');
  svg.removeAttribute('height');
  svg.style.width = '100%';
  svg.style.height = 'auto';
  svg.style.maxWidth = '100%';
  return svg;
}

// Modal state — single shared overlay reused across every diagram on
// the page.
let modalRoot = null;

function ensureModal() {
  if (modalRoot) return modalRoot;

  modalRoot = document.createElement('div');
  modalRoot.className = 'mermaid-modal';
  modalRoot.setAttribute('role', 'dialog');
  modalRoot.setAttribute('aria-modal', 'true');
  modalRoot.setAttribute('aria-label', 'Enlarged diagram');
  modalRoot.hidden = true;

  const surface = document.createElement('div');
  surface.className = 'mermaid-modal-surface';

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'mermaid-modal-close';
  closeBtn.setAttribute('aria-label', 'Close enlarged diagram');
  closeBtn.textContent = 'Close ✕';

  const stage = document.createElement('div');
  stage.className = 'mermaid-modal-stage';

  surface.appendChild(closeBtn);
  surface.appendChild(stage);
  modalRoot.appendChild(surface);
  document.body.appendChild(modalRoot);

  // Element that opened the modal — focus returns here on close so
  // keyboard and screen-reader users don't lose their place on the page.
  // (WCAG 2.4.3 / WAI-ARIA authoring practices for modal dialogs.)
  let triggerElement = null;

  const close = () => {
    modalRoot.hidden = true;
    stage.innerHTML = '';
    document.body.style.overflow = '';
    if (triggerElement) {
      triggerElement.focus();
      triggerElement = null;
    }
  };

  closeBtn.addEventListener('click', close);
  modalRoot.addEventListener('click', (e) => {
    if (e.target === modalRoot) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modalRoot.hidden) close();
  });

  modalRoot._open = (svgMarkup, triggerEl) => {
    triggerElement = triggerEl || document.activeElement;
    stage.innerHTML = svgMarkup;
    const svg = stage.querySelector('svg');
    if (svg) {
      // Different sizing from inline: fill whichever dimension hits first.
      // SVG's preserveAspectRatio="xMidYMid meet" (default) handles the
      // letterboxing, so a portrait diagram uses full height and a
      // landscape one uses full width.
      svg.removeAttribute('width');
      svg.removeAttribute('height');
      svg.style.width = '100%';
      svg.style.height = '100%';
      svg.style.maxWidth = '100%';
      svg.style.maxHeight = '100%';
      svg.style.display = 'block';
    }
    modalRoot.hidden = false;
    document.body.style.overflow = 'hidden';
    closeBtn.focus();
  };

  return modalRoot;
}

async function renderMermaidBlocks() {
  const blocks = document.querySelectorAll('pre[data-language="mermaid"]');
  if (!blocks.length) return;

  for (const [i, pre] of blocks.entries()) {
    const src = extractMermaidSource(pre);
    if (!src) continue;

    // The full expressive-code figure wraps the <pre>; keep that as the
    // "source" block so the copy button, syntax highlighting, and framing
    // survive when the user opens the <details>.
    const figure =
      pre.closest('figure') || pre.closest('.expressive-code') || pre;

    const container = document.createElement('div');
    container.className = 'mermaid-block not-content';

    const rendered = document.createElement('div');
    rendered.className = 'mermaid-rendered';

    // Expand button lives in the corner of the rendered frame.
    const expandBtn = document.createElement('button');
    expandBtn.type = 'button';
    expandBtn.className = 'mermaid-expand';
    expandBtn.setAttribute('aria-label', 'Enlarge diagram');
    expandBtn.title = 'Enlarge diagram';
    expandBtn.innerHTML =
      '<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" fill="currentColor">' +
      '<path d="M4 4h6v2H6v4H4V4zm10 0h6v6h-2V6h-4V4zM4 14h2v4h4v2H4v-6zm14 0h2v6h-6v-2h4v-4z"/>' +
      '</svg>';

    const details = document.createElement('details');
    details.className = 'mermaid-source';
    const summary = document.createElement('summary');
    summary.textContent = 'Show mermaid source';
    details.appendChild(summary);

    // Replace the raw figure with our container first, then move the
    // original figure inside <details> below the rendered diagram.
    figure.replaceWith(container);
    container.appendChild(rendered);
    rendered.appendChild(expandBtn);
    details.appendChild(figure);
    container.appendChild(details);

    try {
      const { svg, bindFunctions } = await mermaid.render(
        `kube-agents-mermaid-${i}-${Date.now()}`,
        src,
      );
      // Insert SVG before the expand button so button stays in the top
      // right when we position it absolutely.
      const svgHost = document.createElement('div');
      svgHost.className = 'mermaid-svg';
      svgHost.innerHTML = svg;
      rendered.insertBefore(svgHost, expandBtn);
      const svgEl = svgHost.querySelector('svg');
      if (svgEl) makeSvgResponsive(svgEl);
      if (bindFunctions) bindFunctions(svgHost);

      expandBtn.addEventListener('click', () => {
        ensureModal()._open(svgHost.innerHTML, expandBtn);
      });
    } catch (err) {
      // On render failure, drop the wrapper and restore the source block
      // so the user still sees something useful.
      console.warn('[mermaid-render] failed to render diagram', err);
      container.replaceWith(figure);
    }
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', renderMermaidBlocks);
} else {
  renderMermaidBlocks();
}
