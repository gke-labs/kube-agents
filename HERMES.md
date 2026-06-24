# Hermes Agent Customizations & Workarounds

This document outlines the customizations, workarounds, and patches applied to the core Hermes Agent framework within the `kube-agents` harness.

---

## 1. Google Chat Outbound Formatting Bug

### Description of the Bug
When sending messages back to users via the Google Chat integration, formatting placeholders (like `GC1`, `GC3`, `GC5`...) were leaking into the final output instead of the unredacted/formatted resource names. This occurred when the text contained nested Markdown formatting constructs (such as bold inline code like `**`broker-dbbb484c5-d8p9w`**`).

### Root Cause
1. In the Google Chat platform adapter (`plugins/platforms/google_chat/adapter.py`), the `format_message(content)` function processes standard Markdown and translates it into Google Chat's specific dialect.
2. To protect formatting regions (such as inline code blocks, links, and bold blocks) from being corrupted by subsequent regex parsing passes, the function temporarily substitutes them with sequential tokens: `\x00GC0\x00`, `\x00GC1\x00`, etc., storing the original content in a `placeholders` dictionary.
3. For nested styling (e.g., a bold block wrapping an inline code block), the inner block is protected first (`\x00GC0\x00`). Then the outer bold block matches the inner placeholder and wraps it, creating a nested dictionary mapping: `\x00GC1\x00` $\rightarrow$ `*\x00GC0\x00*`.
4. When restoring the placeholders at the end of the formatting loop, the code iterated through the dictionary keys in **forward insertion order**:
   * It attempts to restore `\x00GC0\x00` first. However, the text only contains `\x00GC1\x00`, so no replacement occurs.
   * It then restores `\x00GC1\x00` to `*\x00GC0\x00*`.
   * The loop ends, leaving the inner placeholder `\x00GC0\x00` unrestored. Null bytes are stripped during output rendering, causing it to leak to the user as `GC0` (or `GC1`, `GC2`, etc.).

### Proposed Upstream Fix
The proposed upstream fix updates the key traversal order in the restoration loop to restore the placeholders in **reverse order of creation** (using `reversed(list(placeholders.items()))`). This ensures that the outermost nested placeholders are expanded first, exposing the inner placeholders so they can be matched and resolved in subsequent iterations.

* **Upstream Pull Request**: [NousResearch/hermes-agent/pull/51567](https://github.com/NousResearch/hermes-agent/pull/51567)

---

## 2. Temporary Workaround (In-Tree Override)

Until the upstream PR is merged and a new base image tag is released, we have implemented an in-tree override workaround in this repository:

1. **Patched File Location**: [deploy/shared/overrides/google_chat/adapter.py](file:///usr/local/google/home/dmitryshnayder/git/kube-agents/deploy/shared/overrides/google_chat/adapter.py)
   * This contains the full code of the `google_chat` platform adapter with the reverse restoration loop patch applied:
     ```python
     # Restore protected regions in reverse order of creation to handle nested formatting.
     for key, value in reversed(list(placeholders.items())):
         text = text.replace(key, value)
     ```
2. **Build-Time Integration**: The `deploy/docker/Dockerfile` copies this override file into the container image on top of the pip-installed package:
   ```dockerfile
   # Copy custom overrides for core hermes packages
   COPY --chown=hermes:hermes deploy/shared/overrides/google_chat/adapter.py /opt/hermes/plugins/platforms/google_chat/adapter.py
   ```
