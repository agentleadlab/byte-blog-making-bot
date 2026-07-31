# Brand assets

Drop files here and the cover-image renderer picks them up automatically — no
code changes needed.

| File | Effect |
| --- | --- |
| `logo.png` (or `.svg`, `.jpg`) | Replaces the "Agent Lead Lab" text lockup at the top of the cover image. Export at ~2x the rendered height (68px+) so it stays sharp. |
| `fonts/Anton-Regular.woff2` | Any font file in `fonts/` is embedded as an `@font-face`. The family name is derived from the filename (`Anton-Regular.woff2` → `Anton Regular`), so name the file after the family the template asks for: `Anton`, `Oswald`, or `Archivo Black`. |

Without these, the cover still renders — it just uses a text wordmark and
whatever condensed sans the system provides, which is wider than the Canva
original.

Supported font formats: `.woff2`, `.woff`, `.ttf`, `.otf`.
