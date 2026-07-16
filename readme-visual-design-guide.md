# README Visual Design Guide

A reusable playbook for the **visual presentation** of a project's repository —
specifically how to build a polished, "product landing page" README. It captures
the layout patterns, components, and asset conventions used on the reference
project so they can be replayed on new institutional repos.

This is about *how the page looks*, not how the code works. Everything here is
GitHub-flavored Markdown + a little inline HTML (GitHub renders a safe subset).

> **How to use:** copy this file into the new project, then build the README
> top-to-bottom following the **Page Anatomy** order. Snippets are copy-paste
> ready — replace `ALL_CAPS_PLACEHOLDERS` and the image paths.

---

## 1. Principles

1. **The top third sells the project.** Logo, name, tagline, badges, and a hero
   screenshot should all land *above the fold* before any prose.
2. **Center the masthead, left-align the body.** A centered header block reads as
   "brand"; left-aligned running text reads as "docs". Switch deliberately.
3. **Show, don't just tell.** Every major capability earns a screenshot. A
   features list is fine; a features list *with a gallery* is convincing.
4. **One visual rhythm.** Reuse the same table style, caption style, and divider
   placement throughout so the page feels designed, not assembled.
5. **Assets are organized and committed.** Images live in predictable folders
   (`public/brand/`, `docs/screenshots/`) and are referenced by relative path.
6. **Honesty in the visuals.** Sample-data notes and non-affiliation notices sit
   right next to the screenshots/logos they qualify.

---

## 2. Page Anatomy (top → bottom)

The reference order. Sections after the masthead are optional/reorderable, but
this sequence is the default rhythm:

```
┌─ CENTERED MASTHEAD ─────────────────────────┐
│  logo  ·  # Title  ·  ### Tagline           │
│  one-paragraph description                   │
│  badge row                                   │
│  hero screenshot (full width)               │
└─────────────────────────────────────────────┘
───  (horizontal rule)
## Overview            ← the "why", 1–2 short paragraphs
## Features            ← bulleted, bolded lead-ins
## Screenshots         ← 2×N gallery grid + sample-data note
### Theming / Variants ← optional showcase of alternate looks
## Tech stack          ← table
## Project structure   ← annotated file tree
## Getting started     ← prerequisite/step tables
## <Domain sections>   ← e.g. compliance, with seal row + callout
## Acknowledgements    ← data sources, credits
## License             ← short block, links to LICENSE
```

---

## 3. The Centered Masthead

Wrap the whole header in a single centered `<div>`. Logo → H1 → H3 tagline →
description → badges → hero, all inside it.

```html
<div align="center">

<img src="public/brand/LOGO.svg" alt="PROJECT logo" width="76" height="76" />

# PROJECT_NAME

### Short, punchy tagline.

A one-paragraph description in plain prose — what it is and who it's for. Keep it
to 2–3 sentences; this is the elevator pitch, not the Overview section.

<!-- badge row goes here (§4) -->

<br />

<!-- hero screenshot goes here (§5) -->

</div>
```

Notes:
- **Logo:** prefer an **SVG** at `width="76" height="76"`. SVG stays crisp on
  retina and in light/dark. A PNG works if you don't have vector art.
- **Blank lines matter:** keep blank lines between the HTML tags and the
  Markdown (`#`, `###`) so GitHub still parses the Markdown inside the `<div>`.
- **Tagline** uses `###` (not bold text) so it renders as a real subheading.
- Close the centered `<div>` *after* the hero, then drop a `---` rule before the
  body starts.

---

## 4. Badge Row (shields.io)

A single row of [shields.io](https://shields.io) badges directly under the
description. Each badge: a tech/logo, brand-accurate color, and a link.

```markdown
[![Next.js](https://img.shields.io/badge/Next.js-16-000000?logo=nextdotjs&logoColor=white)](https://nextjs.org/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)
```

Badge URL recipe — `badge/LABEL-MESSAGE-HEXCOLOR?logo=SLUG&logoColor=white`:

| Part        | Example          | Notes                                            |
| ----------- | ---------------- | ------------------------------------------------ |
| `LABEL`     | `Next.js`        | Tech name; escape spaces as `%20`                |
| `MESSAGE`   | `16`             | Usually the major version                         |
| `HEXCOLOR`  | `000000`         | Use the tech's **official brand hex**            |
| `logo=`     | `nextdotjs`      | [Simple Icons](https://simpleicons.org) slug     |
| `logoColor` | `white`/`black`  | Pick for contrast against the badge color        |

Conventions:
- **6–8 badges max** — the core stack + a license badge. More becomes noise.
- Order them by importance (framework → language → datastore → tooling → license).
- **Every badge links** to the project's home page; the license badge links to
  `LICENSE`.
- Match each badge color to the real brand color so the row looks intentional.

---

## 5. Hero Screenshot

One full-width screenshot of the product's best view, inside the centered div,
separated from the badges by `<br />`.

```html
<br />

<img src="docs/screenshots/HERO.png" alt="Descriptive alt text of the main view" width="100%" />

</div>

---
```

- `width="100%"` makes it span the content column on every screen size.
- The `alt` text should describe the actual screen ("inventory view with live
  hazard pictograms"), not just the product name — it's accessibility *and* SEO.
- Pick the screen that shows the most value at a glance.

---

## 6. Features List

Left-aligned, bulleted, each with a **bolded lead-in** then an em-dash and the
detail. The bold creates a scannable left edge.

```markdown
- **Hierarchical inventory** — organize items by site, room, and owner, with
  fast search and drill-down navigation.
- **Automatic enrichment** — looks up data from **SOURCE_A** with a **SOURCE_B**
  fallback.
- **Reporting & export** — generate formatted Excel/PDF reports.
```

Keep each bullet to one or two lines. Bold the key *proper nouns* inside the
detail too (data sources, standards) so they pop.

---

## 7. Screenshot Gallery

A 2-column HTML `<table>` grid. Each cell: image, `<br />`, then a `<sub>` caption
with a **bold lead-in**. Tables give you even columns that Markdown image syntax
can't.

```html
<table>
  <tr>
    <td width="50%">
      <img src="docs/screenshots/VIEW_A.png" alt="..." /><br />
      <sub><b>View A</b> — what it shows and why it matters.</sub>
    </td>
    <td width="50%">
      <img src="docs/screenshots/VIEW_B.png" alt="..." /><br />
      <sub><b>View B</b> — one-line description.</sub>
    </td>
  </tr>
  <!-- repeat <tr> for more rows -->
</table>

> Screenshots use generated sample data — no real people, locations, or records.
```

- `<td width="50%">` keeps the two columns even.
- `<sub>` shrinks captions so they read as captions, not body text.
- Always add the **sample-data disclaimer** blockquote right under the gallery if
  the screenshots could be mistaken for real data.

---

## 8. Theming / Variant Showcase

If the product has alternate themes/skins, show them. A full-width screenshot of
the variant plus a `<sub>` caption explaining how to enable it.

```html
### Theming

PROJECT ships with light/dark support and two palettes. The default is **NAME_A**;
an alternate **NAME_B** palette is available via `ENV_FLAG=value`.

<img src="docs/screenshots/theme-variant.png" alt="App in the alternate theme" width="100%" />

<sub><b>NAME_B theme</b> — the same view with <code>ENV_FLAG=value</code>.</sub>
```

Use `<code>` inside captions for env vars/flags so they're visually distinct.

---

## 9. Tech Stack Table

A two-column `Layer | Technology` table, each tech a Markdown link. More scannable
than a paragraph, more structured than badges.

```markdown
| Layer    | Technology                                            |
|----------|-------------------------------------------------------|
| Framework| [Next.js](https://nextjs.org/) + [React](https://react.dev/) |
| Language | [TypeScript](https://www.typescriptlang.org/)         |
| Database | [PostgreSQL](https://www.postgresql.org/)             |
| UI       | [shadcn/ui](https://ui.shadcn.com/) + [Tailwind](https://tailwindcss.com/) |
```

This complements the badge row (which is at-a-glance) with the full, linked
inventory of dependencies.

---

## 10. Annotated Project Structure

A file tree in a fenced code block, with right-aligned inline comments. Show only
the meaningful directories — prune noise.

```markdown
```
project/
├── app/                # Application code
│   ├── (feature-a)/    # Grouped routes / modules
│   └── api/            # Route handlers
├── components/         # Shared UI components
├── lib/                # Domain logic
└── data/               # Static reference datasets
```
```

- Use box-drawing characters (`├──`, `│`, `└──`) — copy them from this template.
- Align trailing `# comments` into a visual column.
- Describe *purpose*, not contents ("Domain logic" beats "ts files").

---

## 11. Reference Tables (prerequisites · config · scripts)

Use the same table style everywhere for consistency. Common ones:

```markdown
| Requirement | Version | Notes                         |
|-------------|---------|-------------------------------|
| Node.js     | 22+     | Use nvm to install            |

| Variable        | Required | Description                  |
|-----------------|----------|------------------------------|
| `DATABASE_URL`  | ✅       | Connection string            |
| `OPTIONAL_KEY`  | —        | Enables optional feature      |

| Script        | Description                  |
|---------------|------------------------------|
| `npm run dev` | Start the dev server         |
```

Conventions:
- **`✅` for required, `—` for optional** in a "Required" column — fast to scan.
- Wrap code/identifiers in `` `backticks` `` inside cells.
- Keep column order stable across tables (name → flag → description).

---

## 12. Collapsible Sections

Long or advanced content goes inside `<details>` so it doesn't bloat the page.
The reader expands it only if they need it.

```html
<details>
<summary><strong>Advanced reference (click to expand)</strong></summary>

...long content, code blocks, configs...

</details>
```

- Bold the `<summary>` so the toggle reads as a heading.
- Keep one blank line after `<summary>` and before `</details>` so Markdown
  inside still renders.
- Good for: lengthy reference configs, optional setups, FAQs.

---

## 13. Credential / Seal Rows + Non-Affiliation Callout

When showing third-party logos (standards bodies, agencies, certifications,
"as seen in"), present them as a centered, linked, evenly-spaced row — and, if
they're not endorsements, immediately disclaim that.

```html
<!--
  Provenance note for maintainers: where these logos came from, licensing,
  and why each was chosen. Keep this as an HTML comment in the source.
-->
<div align="center">

<a href="https://EXAMPLE.org/program"><img src="docs/agency-seals/A.svg" alt="Org A — Program" height="76"></a>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
<a href="https://EXAMPLE.org/program"><img src="docs/agency-seals/B.svg" alt="Org B — Program" height="76"></a>

</div>

> ### ⚠️ Not affiliated with, certified by, or endorsed by any of these organizations
>
> The logos above belong to their respective owners and are shown **solely as
> references** to the public standards this project is designed around. Their use
> does **not** imply review, certification, sponsorship, or endorsement. This is
> an **independent project**.
```

- Uniform `height="76"` on every logo keeps the row balanced even when source
  images differ in size.
- Separate logos with `&nbsp;` runs (≈6) for even spacing.
- Keep an **HTML comment** documenting each logo's source/licensing for future
  maintainers.
- The **non-affiliation callout** is a blockquote with a `### ⚠️` heading so it's
  impossible to miss — essential whenever marks aren't actual endorsements.

---

## 14. Callouts & Disclaimers

Use blockquotes (`>`) for asides, warnings, and disclaimers. They render with a
distinct left border that separates them from body text.

```markdown
> For day-to-day development, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

> This tool surfaces information to assist users. It is **not** a substitute for
> authoritative/official guidance.
```

Put a disclaimer next to the thing it qualifies (under the gallery, under the
seal row, under domain claims), not buried at the bottom.

---

## 15. License Footer

Close with a short, human-readable license block that links to the full text.

```markdown
## License

**Proprietary — All Rights Reserved.** Copyright © YEAR OWNER.

This source is published for reference only; all rights reserved. You may not
use, copy, modify, or distribute it without prior written permission. See
[LICENSE](LICENSE) for full terms.
```

Mirror whatever the license badge in the masthead says — they should agree.

---

## 16. Asset Organization

Where the images the README references actually live:

| Folder                 | Holds                                              |
| ---------------------- | ------------------------------------------------- |
| `public/brand/`        | Logo / wordmark (SVG preferred), favicons         |
| `docs/screenshots/`    | All README screenshots (hero, gallery, themes)    |
| `docs/agency-seals/`   | Third-party logos/seals shown in the README       |

Conventions:
- **Reference by relative path** (`docs/screenshots/x.png`), never an absolute
  URL — so images work in forks and offline clones.
- Name screenshots by what they show (`inventory-hazards.png`, `reports.png`),
  not by order (`screenshot1.png`).
- Commit the images. They're part of the README, not build artifacts.
- Keep raw/source design files (PSD, Figma exports, oversized originals) **out**
  of the repo or in a git-ignored local folder.

---

## 17. Quick Checklist

- [ ] Centered masthead: logo → `#` title → `###` tagline → description
- [ ] Badge row: 6–8 brand-colored, linked shields.io badges + license badge
- [ ] Full-width hero screenshot with descriptive `alt`, then a `---` rule
- [ ] Features list with bolded lead-ins
- [ ] Screenshot gallery as a 2-col `<table>` with `<sub>` captions
- [ ] Sample-data disclaimer under the gallery (if applicable)
- [ ] Theming/variant showcase (if the product has variants)
- [ ] Tech-stack table with linked technologies
- [ ] Annotated project-structure tree
- [ ] Consistent reference tables (`✅`/`—` for required/optional)
- [ ] `<details>` around any long/advanced content
- [ ] Seal row + non-affiliation callout (if showing third-party marks)
- [ ] Disclaimers placed next to what they qualify
- [ ] License footer mirroring the license badge
- [ ] All images committed under `public/brand/` & `docs/`, relative paths
```
