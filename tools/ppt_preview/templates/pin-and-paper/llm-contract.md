# Pin & Paper LLM Contract

Use this contract when asking an LLM to plan Chinese PPT pages in the Pin & Paper visual system.

## Goal

The LLM should output a structured Chinese slide plan that can be rendered into the local Pin & Paper template. It must choose one of the 11 page types in `slide-types.json` and fill that page type's slots from existing OralSage slide content.

All user-visible text MUST be Chinese unless the source input is intentionally English. Stable code fields (`page_type`, `type`, `source_class`) may remain in English.

## Source Of Truth

The LLM may use only the supplied OralSage input:

- `slides.v1.slides[*].title`
- `slides.v1.slides[*].subtitle`
- `slides.v1.slides[*].bullets`
- `slides.v1.slides[*].blocks[*].title`
- `slides.v1.slides[*].blocks[*].body`
- `slides.v1.slides[*].blocks[*].display`
- `slides.v1.slides[*].layout_plan`
- `handout_section.content` (via `slides.v1` projection)

The LLM must NOT:
- Invent new medical facts, citations, diagnoses, treatment claims, statistics, or quiz questions. All medical content must come exclusively from the supplied `slides.v1` / `handout_section` projection. No new medical facts may be introduced.
- Output HTML or CSS
- Reference other template systems from the upstream repository
- Generate content from outline titles alone

## Slot Semantics

### `headline` — SHORT TITLE, NOT BODY SUMMARY

`headline` is the page's main heading.  It MUST be a short, punchy title — prefer the source slide's `title` field (or a concise derivative).  It MUST NOT be a long overview sentence or body paragraph promoted to heading position.

- ✅ Correct: short slide title (~10-15 Chinese characters)
- ❌ Wrong: long overview body text promoted to headline (~40+ chars)

### `eyebrow` — SHORT CATEGORY LABEL ONLY

`eyebrow` is a tiny classification tag (≤20 chars).  It must not carry the real heading or overview content.  Derive it from the slide topic keyword, not from body text.

### `overview` / `body` — BODY / SUBTITLE / INTRO POSITION

Long-form overview paragraphs belong in `subtitle`, `body`, or column/card `body` slots — NEVER promoted to `headline`.

### `meta` — DISTINCT FROM `title`

The `meta` field on cards/columns must NOT duplicate the item's `title`.  If there is no distinct metadata to show, leave `meta` as an empty string `""`.

## Output Format

The LLM outputs `page_type + slots`, NOT HTML/CSS. Return JSON only:

```json
{
  "schema_version": "pin_and_paper_slide_plan.v1",
  "template_slug": "pin-and-paper",
  "deck_title": "",
  "session_id": "",
  "slides": [
    {
      "source_slide_id": "",
      "source_outline_node_id": "",
      "page_type": "cover",
      "rationale": "",
      "slots": {},
      "content_warnings": []
    }
  ],
  "warnings": []
}
```

`page_type` must be one of the 13 types defined in `slide-types.json`:

- `cover`
- `agenda`
- `notes`
- `notes_2up`
- `notes_4up`
- `section`
- `notice`
- `chart`
- `process`
- `matrix`
- `stats`
- `quote`
- `cta`

Slot names and array shapes must follow `slide-types.json` for the chosen `page_type`.

## Two-Phase API Flow (Per Slide)

### Phase 1: Initial Plan

The LLM receives:

- Fixed template name: `pin-and-paper`.
- The `slide-types.json` page-type / slot constraints (compact reference).
- Text budget table with per-slot character limits.
- The current source slide's full `slides.v1` content.
- Rules: "only use input content, no new medical facts", "all visible text in Chinese by default", "return JSON only, no HTML/CSS".

The LLM returns a single slide entry conforming to `pin_and_paper_slide_plan.v1`.

### Phase 2: Over-Budget Full-Page Compression (conditional)

If Python detects that any slot, card, or cell exceeds its text budget after Phase 1, it triggers a second LLM request containing:

1. The original source slide (unchanged).
2. The complete Phase 1 page plan (full JSON).
3. A list of budget failures, each with: the slot path, current character length, and target maximum.
4. The text budget table repeated.
5. Instruction: "return a complete revised page plan, not just the changed fields."

The LLM must return the **entire revised slide entry** — Python replaces Phase 1 with this output.

### After Retry

- Budgets met → plan accepted.
- Budgets still exceeded → Python writes `pin_and_paper_over_budget_after_retry` warning. Plan returned **as-is**. Python and renderer **must never truncate**.
- Renderer may show a visual overflow indicator but must NOT cut text.

### Retry Limit

At most **one** compression retry per slide. No third LLM call for the same slide.

## Information Density Rules

### First-round generation MUST preserve normal teaching information density

- Do NOT pre-compress slide body text to match the English template's extremely short sample text.
- When a page has obvious empty space, do NOT compress body text to be nearly as short as the title.
- The first plan MUST retain normal teaching information: definitions, mechanisms, clinical significance, key distinctions, and common misconceptions.

### Compression is only triggered by real overflow

- Only when budget checks or real rendering checks confirm overflow, occlusion, or unreadable text, trigger a full-page retry compression.
- Compression retry MUST be full-page: pass the original slide, the complete first plan, the specific failing slots, and the budget/overflow reason to the LLM, and ask the LLM to return a full revised page plan.

### Compression must preserve teaching essentials

- Prioritize removing redundant wording over core information.
- Must retain: definitions, mechanisms, clinical significance, key distinctions, common misconceptions.

### Hard boundaries

- Python/JS/renderer must NOT directly truncate text.
- CSS `overflow: hidden` must NOT be used to silently cut body text to pass acceptance.
- If a slot is over-budget after retry, mark it visibly (`over_budget` or `needs_compression_retry`) but render the full text.

## Multi-Point Knowledge Page Routing

When the source slide contains multiple parallel knowledge points:

| Point count | Preferred page types | Rationale |
|-------------|---------------------|-----------|
| 2 | `notes_2up` (required) | Fixed 2-card spacious double-column layout — two equal-weight cards |
| 3 | `notes` or `notice` | 3-column card grid or 2-3 explanatory columns |
| 4 | `notes_4up` (required) | Fixed 2×2 grid — four equal cards, never 3+1 |
| 3-6 | `cta` | Right-panel steps/list — BEST for knowledge points with title + explanation |
| 3-6 | `agenda` | Row list with numbers — good for topic overviews and learning paths |
| 5-6 | `cta` (preferred) | List/steps format with room for explanation per point |
| 3-6 | `process` | Sequential flow with timeline — for ordered steps |

### Routing rules

- **2 key_points → `notes_2up` REQUIRED.** Do NOT use generic `notes` or any other page type.
- **4 key_points → `notes_4up` REQUIRED.** Must be a stable 2×2 grid. Do NOT use generic `notes`, `cta`, or any other page type.
- 3 key_points → `notes` (3-column grid) or `notice` (2-3 explanatory columns).
- Multi-point pages (5+ knowledge points) MUST route to `cta`'s list/steps or `agenda`, NOT to card grids.
- `notes` is for 2-4 cards ONLY and MUST NOT become a universal fallback for all multi-point content.
- `cta` is the **preferred** multi-point candidate for 5+ points (3-6 steps, each with title + explanation body).
- `notes_2up` and `notes_4up` are **fixed page types** — their card counts are baked into the renderer. The LLM must supply exactly 2 cards for `notes_2up` and exactly 4 cards for `notes_4up`.

## Text Budget

Keep text within Pin & Paper's 1920×1080 slide constraints.

### Top-Level Slots

| Slot | Max Chars | Description |
|------|-----------|-------------|
| `eyebrow` | 20 | section eyebrow label |
| `headline` | 40 | main headline (varies by page type) |
| `title` | 30 | cover title (per line) |
| `subtitle` | 60 | subtitle or sub-headline |
| `body` | 200 | narrative body paragraph |
| `quote_text` | 120 | quote main text |
| `attribution_name` | 30 | quote attribution name |
| `attribution_detail` | 60 | quote attribution detail |
| `presenter` | 20 | cover presenter name |
| `presenter_role` | 40 | cover presenter role |
| `date_label` | 30 | cover date label |
| `handwritten_note` | 60 | cover handwritten note |
| `marker` | 30 | section marker text |
| `label` | 30 | section label text |
| `scribble` | 50 | handwritten scribble annotation |
| `footer_left` | 30 | footer left text |
| `footer_right` | 30 | footer right text |

### Card/Item Sub-Slots

| Sub-Slot | Max Chars | Description |
|----------|-----------|-------------|
| `num` (step/card) | 4 | step or card number |
| `label` (agenda/notes) | 20 | agenda item label |
| `title` (card/step/col) | 30 | card or step title |
| `body` (card/step/col) | 200 | card or step body text |
| `meta` (agenda/col) | 30 | metadata label |
| `source` (col) | 40 | source citation |
| `value` (stat) | 12 | stat value |
| `unit` (stat) | 8 | stat unit |
| `criterion` (matrix) | 20 | matrix criterion label |
| `cells` (matrix[*]) | 30 | matrix cell text |
| `legend` (chart[*]) | 30 | chart legend item |
| `series_data[*].label` | 20 | chart series label |
| `series_data[*].values` | 12 | chart series value (per point) |
| `x_labels[*]` | 16 | chart x-axis label |
| `y_labels[*]` | 10 | chart y-axis label |
| `timeline[*]` | 30 | process timeline label |

### Budget Enforcement

- Python planner checks EVERY slot, sub-slot, and cell against this table.
- Any value exceeding its max triggers a single compression retry.
- Retry still over budget → warning recorded, **never truncated**.
- Empty strings are always within budget.
- The LLM should compress proactively. If content is too long, add:
  `"content_warnings": ["compressed_long_source: block_id"]`

## Page Type Selection Rules

### Primary routing

- Use `cover` only for deck or major section openers.
- Use `agenda` for topic overviews, learning paths, or course outlines (3-6 items).
- Use `notes` for 2-4 parallel rules, principles, or misconceptions (generic, auto-adapts card layout). NOT a universal fallback.
- Use `notes_2up` for **exactly 2** parallel knowledge points. Fixed spacious 2-card double-column layout. **REQUIRED when key_points == 2.**
- Use `notes_4up` for **exactly 4** parallel knowledge points. Fixed 2×2 grid, four equal cards. **REQUIRED when key_points == 4.**
- Use `section` for major chapter dividers (ink background + big white headline).
- Use `notice` for one core conclusion + 2-3 explanatory dimensions.
- Use `chart` only when the input includes structured trend/comparison data.
- Use `process` for ordered steps/flow (3-6 items) with timeline.
- Use `matrix` for 2-4 column comparisons (differential, criteria, pros/cons).
- Use `stats` only when the input already contains real numbers or metrics.
- Use `quote` for definitions, principles, or key cited insights.
- Use `cta` for 3-6 knowledge points/action steps with title + explanation. **Preferred multi-point candidate for 5+ points.**

### Anti-defaults

- **Do NOT use `notes` as the default/fallback for any multi-point content.**
- **Do NOT use `notes` for exactly 2 key_points.** Use `notes_2up`.
- **Do NOT use `notes` for exactly 4 key_points.** Use `notes_4up`.
- **Do NOT use `notes_2up` or `notes_4up` for 5-6 knowledge points.** Route to `cta` or `agenda` instead.
- **Do NOT use `notes` for sequential content.** Prefer `process`.
- **Do NOT use `notes` for comparisons.** Prefer `matrix`.
- **Do NOT use `insights`-style card grids as a universal catch-all.**
- **Do NOT pre-compress teaching content to match the short English template samples.**

### Deck-level diversity

- Two consecutive slides SHOULD NOT use the same card layout pattern.
- If the planner detects consecutive identical layouts, emit:
  `"content_warnings": ["layout_diversity: consecutive_same_layout slide_N slide_N+1"]`
- This is a **soft constraint** — prefer diversity but don't force inappropriate page types.

## Visual Rules

- Keep the yellow paper background, paper-grain texture, and safety-pin SVG illustrations from `template.html` and `design.md`.
- Keep the original font configuration: Space Grotesk (headlines/body), Caveat (handwritten annotations), DM Mono (labels/meta).
- Do NOT introduce new colors, gradients, image-heavy layouts, or unrelated visual motifs.
- Do NOT invert text on cream/paper fills.
- Do NOT create more cards than the selected page type supports.
- Do NOT output raw HTML or CSS.
- Do NOT reference other templates from the upstream repository.

## Renderer Boundary

The renderer maps `page_type + slots` into HTML/CSS patterns from `template.html`. The LLM is responsible for choosing and filling slots; the renderer is responsible for visual layout.  The renderer produces fixed 16:9 slides (1920×1080) with NO internal scrolling anywhere on any slide.  The renderer must NOT:

- Truncate text
- Hide overflow with CSS clipping (`overflow: hidden` on body text)
- Add `overflow-y: auto` or any scroll container inside a slide
- Generate new content
- Fall back to a different template
- Pre-compress content to fit

If content is too long for the fixed canvas, the LLM MUST handle it at plan time — by choosing a more spacious page_type, reducing per-item count, or compressing body text.  The renderer will NOT rescue an overfull plan.

If a plan carries an `over_budget_after_retry` warning, the renderer may show a visible `over_budget` marker on the affected slot but MUST render the full slot text.

## Stable Prompt Skeleton

Use this structure when calling an LLM:

```text
You are planning slides for the Pin & Paper template only.
Read slide-types.json and obey llm-contract.md.
Use only the provided OralSage slides.v1 input as content truth.
Return JSON only in schema pin_and_paper_slide_plan.v1.
Do not invent medical facts — all medical content must come exclusively from the supplied slides.v1 / handout_section projection.
Do not output HTML/CSS.
All user-visible text must be in Chinese.

Input:
<slides.v1 JSON or selected slide JSON>
```
