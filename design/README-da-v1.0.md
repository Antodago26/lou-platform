# Handoff: bonhome.ch — Direction artistique & design system

## Overview

This handoff contains the complete visual identity and design system for **bonhome.ch**, a Swiss-Romand real-estate platform (location + achat) whose core experience is a conversational assistant called **Lou** that helps users find a home through natural-language dialogue instead of filter grids.

The design establishes:
- A restrained, editorial visual language (crème paper, serif display, warm inks)
- A semantic color system built on the NCS 2026 palette (Teal + Terracotta + Sauge + Bleu d'encre)
- A full component library (wordmark, buttons, inputs, cards, chat bubbles)
- Three key screens (home hero, listing, chat onboarding)
- A microcopy/tone-of-voice system for Lou (tu, warm, concrete)

## About the Design Files

**The files in this bundle are design references created in HTML — prototypes and a navigable design-system document showing the intended look, rules, and behavior. They are not production code to copy directly.**

Your task is to **recreate these HTML designs in the target codebase's existing environment** (React, Next.js, Vue, Svelte, Astro, or whatever the bonhome.ch repo uses) using its established patterns, component library, and routing. If no environment exists yet, choose the most appropriate framework for a content-driven marketing + conversational product (e.g., Next.js App Router + Tailwind or vanilla CSS vars).

The `tokens.css` file **is** production-ready and can be dropped in as-is.

## Fidelity

**High-fidelity (hifi).** Every color is a final hex value, every font size/weight/tracking is specified, every radius and shadow is tokenized. Recreate the UI pixel-perfectly using the codebase's existing libraries and patterns — but translate HTML→components, CSS→whatever the stack uses (CSS modules, Tailwind, styled-components, etc.).

## File Structure

```
design_handoff_bonhome_website/
├── README.md                                ← this file
├── tokens.css                               ← production-ready CSS variables
├── Direction Artistique bonhome.ch.html     ← navigable design system (open in browser)
├── logos/
│   ├── wordmark.svg                        ← main wordmark (ink + sage chevron)
│   ├── wordmark-dark.svg                   ← on dark background
│   ├── wordmark-mono.svg                   ← monochrome (currentColor)
│   ├── favicon.svg                         ← 32×32, scales to 16×16
│   └── og-card.svg                         ← 1200×630 social share card
└── reference_designs/
    ├── Branding bonhome D1 V5P.html        ← final validated palette exploration
    └── Branding bonhome D1 V5.html         ← previous iteration (sage-forward)
```

**Start with `Direction Artistique bonhome.ch.html`** — it's the canonical reference. Open it in a browser; the left nav jumps to every token and component.

## Design Tokens (full reference in `tokens.css`)

### Colors

**Surfaces (warm cream neutrals — never use cool grays):**
| Token | Hex | Use |
|---|---|---|
| `--bh-paper` | `#F5F1EA` | page background |
| `--bh-paper-soft` | `#EDE8DF` | alternating sections |
| `--bh-paper-sunk` | `#E4DED2` | sunk cards / input backgrounds |
| `--bh-paper-raised` | `#FDFBF6` | raised cards / modals |

**Inks (text — warm-toned):**
| Token | Hex | Use |
|---|---|---|
| `--bh-ink` | `#1A2332` | body & titles |
| `--bh-ink-soft` | `#4A5568` | subtitles, labels |
| `--bh-ink-mute` | `#6B7280` | metadata, captions |

**Brand anchors:**
| Token | Hex | Role | Rule |
|---|---|---|---|
| `--bh-teal` / `--bh-brand` | `#1F6B6E` | primary CTAs, links, active nav | 20% of screen |
| `--bh-terracotta` / `--bh-accent` | `#C15A3E` | likes, matches, warmth signals | ~8% of screen |
| `--bh-sage` / `--bh-signature` | `#6B8A74` | **wordmark only** (chevron + dot) | never used elsewhere |

**Full palette incl. tints/deeps** — see `tokens.css`.

### Typography

- **Fraunces** (serif, weight 300/400) — display, titles, wordmark, notable numbers
- **Inter** (sans, weight 400/500/600) — body, UI, nav, buttons

**⚠️ Critical:** Fraunces **must** be loaded with the `opsz` axis, and every Fraunces usage must set `font-variation-settings: "opsz" 144;`. Without it, Fraunces renders thin like Times.

Font loading:
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300;0,9..144,400;0,9..144,500;1,9..144,300;1,9..144,400&family=Inter:wght@400;500;600&display=swap">
```

Type scale (all `clamp()`-based, see tokens.css):
| Size | Range | Use |
|---|---|---|
| `--bh-text-display` | 48–88px | hero |
| `--bh-text-3xl` | 40–60px | h1 |
| `--bh-text-2xl` | 32–44px | h2 |
| `--bh-text-xl` | 24–32px | h3 |
| `--bh-text-base` | 16–17px | body |
| `--bh-text-sm` | 14–15px | small |
| `--bh-text-xs` | 12–13px | captions, labels |

Tracking: `-0.035em` for wordmark, `-0.02em` for h1/h2, `0.12em` for uppercase labels.

### Spacing — 4pt scale
`--bh-space-1` (4px) through `--bh-space-10` (128px). Never use arbitrary values.

### Radii
`--bh-radius-xs` (4) / `-sm` (8) / `-md` (12) / `-lg` (20) / `-xl` (28) / `-pill` (999).

### Shadows
`--bh-shadow-xs` (subtle hover) / `-sm` (cards) / `-md` (dropdowns, popovers). Never go heavier — the visual language is flat.

## Screens / Views

### 1. Home

**Purpose:** first impression, establish tone, convert to Lou onboarding.

**Layout:**
- Full-width nav bar, 24px top padding, 1px bottom border `--bh-line`
- Hero section, centered content, max-width 1200px, `--bh-space-9` vertical padding
- Display H1 in Fraunces 300 (`--bh-text-3xl` to `--bh-text-display`), italic subtitle in Fraunces 300 below
- CTA row: primary teal button + ghost button, 12px gap

**Components:**
- **Nav:** wordmark left, links right (Inter 14px, `--bh-ink` active, `--bh-ink-soft` inactive)
- **H1:** "Trouver un chez-soi, plus doucement." Fraunces 300, `text-wrap: balance`
- **Subtitle:** "Lou comprend ce que vous cherchez — même quand les mots manquent." Fraunces 300 italic, `--bh-ink-soft`
- **CTAs:** `Commencer` (primary teal) + `Voir les annonces` (ghost)

### 2. Listing

**Purpose:** browse matches. Horizontal card list, density matters.

**Layout:**
- Uppercase label count at top ("12 biens trouvés · Neuchâtel") — `--bh-text-xs`, tracking 0.1em
- Vertical stack of listing cards, 12px gap

**Listing card:**
- Grid: `80px 1fr auto`, 16px gap, 16px padding
- Thumbnail: 80×80, `--bh-radius-sm`, gradient placeholder
- Title: Fraunces 400, 16px
- Desc: Inter 13px, `--bh-ink-soft`
- Price: Fraunces 400, 18px, `--bh-teal-deep`
- Location: Inter 11px uppercase, tracking 0.05em, `--bh-ink-mute`
- Heart icon: 32×32 circle, outlined `--bh-line` (unliked) or filled `--bh-terracotta` (liked)

### 3. Chat (Lou onboarding)

**Purpose:** conversational intake replacing filter forms.

**Layout:**
- Vertical flex, max-width 520px
- Bubbles alternate left (Lou) and right (user)
- 8px vertical spacing between bubbles

**Chat bubbles:**
- Lou: `--bh-teal-tint` bg, border-radius `20px 20px 20px 4px` (sharp bottom-left), align-left
- User: `--bh-paper-sunk` bg, border-radius `20px 20px 4px 20px` (sharp bottom-right), align-right
- Padding 12px 16px, font-size 15px, line-height 1.4
- Entry animation: 6px translateY + opacity, 280ms `cubic-bezier(0.22, 1, 0.36, 1)`
- Max 75% width

## Components (detailed specs in the navigable HTML)

### Wordmark
- Must be inline HTML+SVG, **never a raster image**
- Three micro-touches: Fraunces 400 ink, sage chevron above the **2nd "o"** (not 1st), sage point at end
- Min size: 16px. Below that, use favicon.
- Never bold, italic, or colored differently (outside the dark/mono variants)

### Buttons
- All pill-shaped (`--bh-radius-pill`), Inter 500 15px, 12×24px padding, 140ms transitions
- **Primary:** `--bh-teal` bg, `--bh-paper` text → hover `--bh-teal-deep`
- **Secondary:** transparent, 1px `--bh-teal` border → hover `--bh-teal-tint` bg
- **Ghost:** transparent, `--bh-teal` text, 12×16px padding → hover `--bh-teal-tint` bg
- **Accent:** `--bh-terracotta` bg (reserved for warmth signals: like, match, coup de cœur — never generic actions)
- All elements: `--bh-focus-ring` on `:focus-visible`

### Inputs
- 12×16px padding, `--bh-paper-raised` bg, 1px `--bh-line` border, `--bh-radius-md`
- Focus: border → `--bh-teal`, box-shadow `--bh-focus-ring`
- Label above, Inter 500 12px, `--bh-ink-soft`
- Placeholder color: `--bh-ink-disabled`

### Cards
- `--bh-paper-raised` bg, 1px `--bh-line` border, `--bh-radius-lg`, `--bh-shadow-xs`
- 24px padding
- H3 in Fraunces 400 20px, p in Inter 14px `--bh-ink-soft`

### Chips
- Pill shape, 4×10px padding, Inter 500 11px tracking 0.05em
- Default: `--bh-teal-tint` bg, `--bh-teal-deep` text
- Accent variant: `--bh-terracotta-tint` bg, `--bh-terracotta-deep` text

## Interactions & Behavior

- **Transitions:** 140ms (fast), 240ms (base), 420ms (slow), all with `cubic-bezier(0.22, 1, 0.36, 1)`
- **Hover:** all interactive elements must have a visible hover state (color shift or bg tint)
- **Focus:** `--bh-focus-ring` (teal translucent 3px ring) on all focusable elements via `:focus-visible`
- **Chat entry animation:** see Chat bubbles section
- **Like button:** outline → filled toggle, instant (no transition on fill — just swap)
- **Respect `prefers-reduced-motion`:** wrap all transitions and `bubble-in` keyframes

## Tone of Voice — Lou (microcopy system)

Lou is an AI assistant that guides users through real-estate search. **She speaks French using `tu`, never `vous`.** Short sentences, active verbs. Never administrative, never superlative-filled. Each message invites or proposes — never commands.

**Do:**
- "Salut. Dis-moi ce que tu cherches, j'écoute."
- "Rien qui colle encore. On élargit un peu ?"
- "Celui-là pourrait te plaire — regarde."
- "Ah. On a perdu le fil une seconde. On retente ?"

**Don't:**
- "Bienvenue ! Veuillez décrire votre recherche."
- "Aucune annonce ne correspond à vos critères."
- "Une erreur est survenue."
- "Êtes-vous sûr ?"

**Emoji:** allowed in Lou's chat messages (max 1 per message). **Never in general UI** (nav, buttons, labels, errors).

**CTA wording:** infinitive verb, no exclamation. "Commencer", "Voir l'annonce", "Envoyer", "Continuer". Destructive actions must state the object: "Supprimer cette alerte" not "Supprimer".

## State Management (conversational flow)

The chat onboarding is a state machine. Suggested states:
- `idle` → Lou greets
- `collecting_type` → "À louer ou à acheter ?"
- `collecting_location` → "Tu vises quelle ville ?"
- `collecting_size` → "Combien de pièces ?"
- `collecting_timing` → "Tu vises quand ?"
- `collecting_budget` → range
- `showing_results` → transition to listings
- `error` → "Ah. On a perdu le fil une seconde. On retente ?"

User answers are parsed with light NLP (or button shortcuts on mobile). Each Lou turn should feel spoken, not formal.

## Accessibility

- All documented text/bg pairs pass WCAG AA minimum (see the contrastes table in the HTML doc)
  - `ink` on `paper` → 12.8:1 (AAA)
  - `teal` on `paper` → 6.1:1 (AA)
  - `terracotta` on `paper` → 4.7:1 (AA for text ≥14px)
- Focus ring visible on every interactive element
- Touch targets ≥ 44×44px on mobile
- Respect `prefers-reduced-motion`
- Semantic HTML: `<nav>`, `<main>`, `<article>` for listings, `<button>` not `<div onclick>`
- Form inputs always have associated `<label>`

## Responsive Behavior

Breakpoints:
- `sm` 480px · `md` 768px · `lg` 1024px · `xl` 1280px · `2xl` 1536px

- **Mobile (< 768px):** single column, 24px gutter, sticky nav collapses to hamburger
- **Tablet (768–1023px):** 2-column listings, 32px gutter
- **Desktop (1024+):** 3-column listings, 48px gutter, chat panel max-width 520px
- All typography uses `clamp()` — already handled in `tokens.css`

## Assets

- **Logo SVGs:** in `logos/`, all hand-authored (inline-editable text + path)
- **No images provided:** listing thumbnails in the mocks use gradient placeholders. The real site will pull property photos from the CMS/API. Use `aspect-ratio: 4/3` and `object-fit: cover` as default.
- **No icons provided:** for heart/like, use an inline SVG; for nav, the design uses text-only links (no icon font).

## Integration Checklist

- [ ] Fraunces loaded with `opsz` axis
- [ ] `tokens.css` imported globally
- [ ] `font-variation-settings: "opsz" 144;` on all Fraunces usages
- [ ] Focus ring visible on every interactive element
- [ ] AA contrast validated on all screens
- [ ] Favicon SVG installed
- [ ] OG card configured in `<head>` meta tags
- [ ] Wordmark rendered inline SVG (never PNG/JPG)
- [ ] `prefers-reduced-motion` respected
- [ ] Tested at 375px, 768px, 1280px minimum
- [ ] Lou copy reviewed — no `vous`, no "veuillez", no superlatives creux

## Support Files

- **`Direction Artistique bonhome.ch.html`** — the single source of truth. Every token, every component, every rule is visible and copyable.
- **`reference_designs/`** — earlier palette explorations kept for context (V5P is the validated one; the green V5 was superseded).
