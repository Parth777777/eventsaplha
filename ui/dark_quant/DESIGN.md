# Design System Document: Precision Quant Editorial

## 1. Overview & Creative North Star
### Creative North Star: "The Obsidian Lens"
This design system is not a generic dashboard; it is a high-performance instrument. We are moving away from the "SaaS Blue" template look toward an **Obsidian Lens**—a dark, multi-layered environment that feels like a physical piece of high-tech hardware. 

The aesthetic is driven by **Intentional Density**. We do not fear data; we embrace it through rigorous editorial hierarchy. By using dramatic typographic scales and tonal depth instead of structural lines, we create an experience that feels authoritative, silent, and immensely powerful. We break the grid through asymmetrical data callouts and overlapping "glass" layers to ensure the interface feels custom-built for elite quantitative analysis.

---

## 2. Colors & Chromatic Depth
Our palette is rooted in the "Dark Quant" philosophy: deep, nocturnal foundations punctuated by high-frequency accents.

*   **Foundation:** The core is `background` (#10141a). Use `surface_container_lowest` (#0a0e14) for deep "wells" of data and `surface_container_highest` (#31353c) for elevated, interactive elements.
*   **The Alpha (Brand):** Use `primary` (#adc6ff) and `primary_container` (#4d8eff). This Electric Blue should be used sparingly to guide the eye toward "Alpha" opportunities.
*   **The Sentiment Accents:** 
    *   **Positive:** `secondary` (#4edea3) — Emerald Green for success.
    *   **Negative:** `tertiary` (#ffb2b7) and `error` (#ffb4ab) — Rose Red for volatility.
    *   **Neutral/Warning:** Use `surface_bright` (#353940) for neutral data points.

### The "No-Line" Rule
**Strict Prohibition:** Do not use 1px solid borders to define sections. This is the hallmark of "template" design.
Instead, boundaries must be defined through **Background Color Shifts**. To separate a sidebar from a main feed, place a `surface_container_low` panel against a `surface` background. The transition of color is the border.

### The "Glass & Gradient" Rule
To add soul to the data, use Glassmorphism on floating elements. Apply `surface_container` colors at 70% opacity with a `backdrop-filter: blur(12px)`. For primary CTAs, use a subtle linear gradient from `primary` to `primary_container` at a 135-degree angle to provide a machined, metallic sheen.

---

## 3. Typography: Editorial Authority
We utilize a pairing of **Space Grotesk** for structural headlines and **Inter** for high-readability data.

*   **Display & Headlines:** Use `display-lg` down to `headline-sm` in **Space Grotesk**. Its geometric quirks lend a "high-tech" editorial feel. 
*   **The Data Layer:** Use `body-md` and `label-md` in **Inter**.
*   **Numerical Precision:** For all "Alpha Scores" and quantitative metrics, use a monospaced variant (or Inter with `font-variant-numeric: tabular-nums`). 
*   **Hierarchy through Weight:** Use `font-weight: 700` for `title-sm` metrics and `font-weight: 300` for supporting labels to create a sharp, high-contrast texture.

---

## 4. Elevation & Depth: Tonal Layering
Depth is achieved through "stacking" the `surface-container` tiers, mimicking physical layers of obsidian.

*   **The Layering Principle:** 
    *   Base Layer: `background`
    *   Section Layer: `surface_container_low`
    *   Component/Card Layer: `surface_container` or `surface_container_highest`
*   **Ambient Shadows:** If a floating element (like a context menu) requires a shadow, use a large blur (24px+) with 4% opacity, using the `on_surface` color as the shadow tint. It should look like a soft glow, not a dark smudge.
*   **The "Ghost Border" Fallback:** If accessibility requires a container boundary, use a **Ghost Border**: `outline_variant` (#424754) at 15% opacity. It should be felt, not seen.

---

## 5. Components & High-Density UI

### Cards & Data Modules
*   **Rule:** Forbid divider lines within cards.
*   **Structure:** Use `spacing-4` (0.9rem) to separate internal groups.
*   **Styling:** Use `roundedness-md` (0.375rem). A card should use `surface_container` against the `background`.
*   **Sentiment Bars:** Progress bars for sentiment should use `secondary` (Positive) or `tertiary` (Negative) with a background of `surface_container_highest`.

### Buttons
*   **Primary:** A gradient of `primary` to `primary_container`. No border. `roundedness-sm`.
*   **Secondary:** `surface_container_highest` background with `on_surface` text. 
*   **Tertiary:** Text-only using `primary_fixed_dim`. 

### Inputs & Quants
*   **Text Fields:** Use `surface_container_lowest` for the input field background to create a "recessed" look. 
*   **Focus State:** A 1px Ghost Border using `primary` at 40% opacity.

### Glowing Map Markers
*   For geographical data or node maps, use `primary` with a `box-shadow: 0 0 12px` using the `primary` color. This "active signal" effect emphasizes the high-tech atmosphere.

---

## 6. Do’s and Don'ts

### Do:
*   **Use Asymmetry:** Place a large `display-sm` metric in the top-left of a card with small `label-sm` data points clustered in the bottom-right.
*   **Embrace Negative Space:** Use `spacing-16` (3.5rem) between major sections to let the data breathe.
*   **Tabular Numerals:** Always ensure numbers in a list align vertically by using monospaced settings.

### Don't:
*   **Don't use pure white (#FFFFFF):** Always use `on_surface` (#dfe2eb) or `on_surface_variant` (#c2c6d6) for text to prevent eye strain in dark environments.
*   **Don't use standard shadows:** If a card looks like it’s "pasted on," your tonal layering has failed. Adjust the `surface_container` tier instead.
*   **Don't use Rounded-Full for everything:** Stick to `roundedness-sm` or `none` for a more "professional/industrial" feel. Save `roundedness-full` only for status chips.

### Interaction Note
When a user hovers over a data card, transition the background from `surface_container` to `surface_bright` and increase the opacity of the Ghost Border to 30%. This subtle "light up" creates a sense of tactile feedback.