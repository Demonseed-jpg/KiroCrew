/**
 * Map a crewmate's DASHBOARD PAGE colour tokens onto the CHAT column's CSS
 * variables, so the chat and the Dashboard tab beside it read as one theme.
 *
 * The problem this solves: a crewmate's page can declare its own colours
 * (`theme.tokens`, validated in `artifact_store/dashboard_package.py`), but the
 * chat in the middle stays on the app theme — a strongly coloured page and a
 * default-dark chat look like two different apps. This module takes the two
 * page colours the chat can safely follow and returns the variable overrides
 * the column sets inline.
 *
 * WHY ONLY TWO TOKENS, NOT THE PAGE'S WHOLE PALETTE: a page's tokens are
 * free-form custom-property names a page author picks (up to 64 of them), with
 * no guaranteed meaning. Copying arbitrary names onto the chat's own variables
 * would be guessing. The chat follows exactly the two whose role is
 * unambiguous — the page BACKGROUND (`--bg`) and the page ACCENT (`--accent`),
 * the same names the app's own theme uses — and derives everything else itself.
 * A page that declares neither keeps today's chat untouched.
 *
 * WHY A WHOLE MINI-PALETTE IS DERIVED, NOT JUST `--bg`/`--text`: the chat column
 * is painted through several surface variables, not one. Bubbles read `--card`,
 * their edges `--border`, secondary labels `--muted`/`--text-strong`, the
 * composer's chips `--bg-hover`/`--bg-elevated`. Overriding only `--bg` and
 * `--text` would leave those sibling surfaces on the app theme's values —
 * bubbles that vanish into a recoloured background, and a `--muted` label at an
 * unmeasured contrast on the new surface. So a recoloured background derives a
 * self-consistent set: the card and chrome surfaces step off the page
 * background toward its text colour, the border is that text at low alpha, and
 * every foreground is paired with the surface it sits on.
 *
 * WHY TEXT IS DERIVED, NOT READ: readability cannot be left to a page author. A
 * declared text colour could sit at any contrast on the declared background. So
 * every foreground here is chosen by measured contrast (`lib/iconContrast`, the
 * repo's core WCAG helpers): black or white, whichever has more contrast on the
 * surface it labels. Against any sRGB colour the better of pure black and pure
 * white never drops below ~4.58:1, so body text clears 4.5:1 on a light page
 * and a dark one alike.
 *
 * WHY NON-OPAQUE TOKENS ARE REFUSED: a translucent `--bg` (e.g. `rgba(0,0,0,0)`)
 * lets the app background show through, so a contrast measured against the token
 * alone is a fiction. A token the chat cannot paint a solid surface from is
 * treated as absent, and the chat keeps its own theme.
 */
import { contrastRatio, parseCssColor, relativeLuminance } from '../../lib/iconContrast'

/**
 * The page colour tokens the chat follows, keyed by CSS custom-property name as
 * the server's `page_theme` emits them. Free-form elsewhere; the chat reads
 * exactly these two.
 */
export interface PageColorTokens {
  /** The page's `--bg`: the surface the chat paints its background from. */
  '--bg'?: string
  /** The page's `--accent`: the chat's accent (caret, links, send). */
  '--accent'?: string
}

/** The chat column's CSS-variable overrides, ready to spread onto an element's style. */
export type ChatThemeVars = Record<string, string>

/** WCAG AA for normal text; stated here so the contract is visible at the call site. */
export const CHAT_AA_TEXT = 4.5

/** The two ends of the tone scale, as readable foregrounds. */
const WHITE = '#ffffff'
const BLACK = '#000000'

interface Readable { color: string; ratio: number }

/**
 * A channel clamped to the 0–255 the browser actually paints. A page token can
 * be a valid-but-out-of-range CSS colour (`rgb(0,0,500)`), which `parseCssColor`
 * returns verbatim; measuring contrast against the raw channel describes a
 * colour the screen never shows, so every channel is clamped before luminance.
 */
function clampCh(n: number): number {
  return Math.max(0, Math.min(255, n))
}

/** WCAG relative luminance of a triple, clamped to what the browser paints. */
function lumOf(r: number, g: number, b: number): number {
  return relativeLuminance(clampCh(r), clampCh(g), clampCh(b))
}

/**
 * The readable text colour for an OPAQUE background triple: black or white,
 * whichever has the higher contrast, with the ratio so a caller can refuse a
 * surface whose best text still falls short.
 */
function readableFor(r: number, g: number, b: number): Readable {
  const bg = lumOf(r, g, b)
  const onWhite = contrastRatio(relativeLuminance(255, 255, 255), bg)
  const onBlack = contrastRatio(0, bg)
  return onWhite >= onBlack ? { color: WHITE, ratio: onWhite } : { color: BLACK, ratio: onBlack }
}

/** The WCAG contrast of colour *fg* (as a triple) on background luminance *bgL*. */
function contrastOn(fr: number, fg: number, fb: number, bgL: number): number {
  return contrastRatio(lumOf(fr, fg, fb), bgL)
}

/**
 * The readable text colour for a CSS colour string, or `null` when it does not
 * parse or is not opaque — an unknown or see-through colour is unusable, not
 * guessed. Exported for the unit test.
 */
export function readableText(bg: string): Readable | null {
  const c = parseCssColor(bg)
  if (!c || c.a < 0.999) return null
  return readableFor(c.r, c.g, c.b)
}

/** Mix two 0–255 channels by *t* (0 = a, 1 = b). */
function mix(a: number, b: number, t: number): number {
  return Math.round(a + (b - a) * t)
}

/** `#rrggbb` for a triple. */
function hex(r: number, g: number, b: number): string {
  return '#' + [r, g, b].map((n) => Math.max(0, Math.min(255, n)).toString(16).padStart(2, '0')).join('')
}

/**
 * The chat column's variable overrides for a crewmate's page colours, or an
 * EMPTY object when the page drives nothing (no tokens, a non-opaque token, or
 * a background whose best text cannot clear 4.5:1 — the fail-safe is today's
 * look, never unreadable text).
 *
 * An empty result is the default-OFF state expressed as data: the column
 * spreads `{}`, which adds no inline variable, so the chat is byte-for-byte the
 * one on `main`.
 */
export function chatThemeVars(tokens: PageColorTokens | null | undefined): ChatThemeVars {
  if (!tokens) return {}
  const vars: ChatThemeVars = {}
  // The luminances of the chat surfaces an accent-coloured TEXT (a reply link,
  // the caret) sits on, filled in by the background branch. Empty when the page
  // declares no readable background, in which case the accent is only checked
  // against the app's own surfaces it leaves in place.
  let chatSurfaceLums: number[] = []

  const bgStr = tokens['--bg']
  const bg = bgStr ? parseCssColor(bgStr) : null
  if (bgStr && bg && bg.a >= 0.999) {
    const text = readableFor(bg.r, bg.g, bg.b)
    if (text.ratio >= CHAT_AA_TEXT) {
      // The page background, and foregrounds derived for it.
      vars['--bg'] = bgStr
      vars['--text'] = text.color
      vars['--text-strong'] = text.color
      const tc = text.color === WHITE ? 255 : 0
      const bgL = lumOf(bg.r, bg.g, bg.b)
      // One step toward the text colour by *t*, used for the muted label and the
      // chrome surfaces below.
      const step = (t: number) => hex(mix(bg.r, tc, t), mix(bg.g, tc, t), mix(bg.b, tc, t))
      const lumAt = (t: number): number => { const s = parseCssColor(step(t))!; return lumOf(s.r, s.g, s.b) }
      // Chrome surfaces step OFF the background toward its text colour, so a
      // bubble, a hovered row and an elevated chip are distinct from the column
      // instead of melting into it. EACH step is CAPPED so the surface's own
      // normal text (`--text`, the chosen extreme) still clears AA on it: a
      // mid-tone page background has no room to lighten a surface and keep that
      // text readable, so there the step shrinks toward 0 and the visible edge
      // below carries the separation instead. The elevated surface is capped
      // INDEPENDENTLY (not derived from the card step), because it steps further
      // and would otherwise drop `--text` below the floor on a mid-tone page.
      const fgLum = text.color === WHITE ? relativeLuminance(255, 255, 255) : 0
      const surfaceFgOk = (t: number): boolean => contrastRatio(lumAt(t), fgLum) >= CHAT_AA_TEXT
      const cardStep = [0.08, 0.05, 0.03, 0].find(surfaceFgOk) ?? 0
      const elevatedStep = [0.12, 0.08, 0.05, 0.03, 0].find(surfaceFgOk) ?? 0
      vars['--card'] = step(cardStep)
      vars['--card-fg'] = text.color
      vars['--bg-hover'] = step(Math.min(cardStep, 0.06))
      vars['--bg-elevated'] = step(elevatedStep)
      // Secondary text (`--muted`): the readable end, pulled toward the
      // background so it reads as muted, but only as far as still clears AA on
      // EVERY surface it renders on (`--bg`, `--card`, `--bg-elevated`). The
      // muted colour sits between the background and the text extreme, so its
      // worst contrast is against whichever surface is stepped FURTHEST toward
      // that extreme — the elevated one — but it is cheapest just to require AA
      // on all three. A mid-tone page leaves little room, so the muting backs off
      // toward the full text colour rather than ship a dim, unreadable label.
      const surfaceLums = [bgL, lumAt(cardStep), lumAt(elevatedStep)]
      const mutedReadable = (t: number): boolean => {
        const m = parseCssColor(step(t))!
        const ml = lumOf(m.r, m.g, m.b)
        return surfaceLums.every((sl) => contrastRatio(ml, sl) >= CHAT_AA_TEXT)
      }
      vars['--muted'] = step([0.66, 0.78, 0.9, 1].find(mutedReadable) ?? 1)
      // The edge: the text colour at low alpha, so a bubble is outlined and
      // reads the same on a light page and a dark one even when the card sits
      // close to the background.
      vars['--border'] = text.color === WHITE
        ? 'rgba(255,255,255,0.22)'
        : 'rgba(0,0,0,0.22)'
      // The composer is a Liquid-Glass pane whose tint is a FIXED per-theme
      // rgba (`--glass-tint*`), anchored on the app's own `--bg` rather than
      // this one — so without this the composer keeps the app theme's light or
      // dark glass and the derived `--text` is measured against the wrong
      // surface. Re-anchor the whole tint ladder on the page background at the
      // same alphas the theme uses, so the composer paints from this palette and
      // `--text` stays readable on it. Each `--bg`-derived tint is opaque enough
      // that `--text` (already AA on `--bg`) clears AA on it too.
      const tint = (a: number) => `rgba(${clampCh(bg.r)},${clampCh(bg.g)},${clampCh(bg.b)},${a})`
      vars['--glass-tint-ultrathin'] = tint(0.72)
      vars['--glass-tint-thin'] = tint(0.78)
      vars['--glass-tint-regular'] = tint(0.84)
      vars['--glass-tint-thick'] = tint(0.9)
      vars['--glass-tint-ultrathick'] = tint(0.95)
      vars['--glass-tint-step'] = tint(0.78)
      vars['--glass-tint'] = tint(0.78)
      // The surfaces an accent-coloured TEXT (reply link, caret) lands on, for
      // the accent-as-text check below: the chat background, a bubble's card,
      // and the elevated surface.
      chatSurfaceLums = [bgL, lumAt(cardStep), lumAt(elevatedStep)]
    }
  }

  const accentStr = tokens['--accent']
  const accent = accentStr ? parseCssColor(accentStr) : null
  if (accentStr && accent && accent.a >= 0.999) {
    const label = readableFor(accent.r, accent.g, accent.b)
    // The accent is used TWO ways and must clear AA for both before the chat
    // follows it, or one of them is left unreadable:
    //   1. as a FILLED surface (send button, pills) — its own label must be
    //      readable on it (`--accent-fg`);
    //   2. as TEXT (`text-accent` reply links, the caret) on the chat surfaces —
    //      so the accent itself must clear AA on the page background and card.
    // If it fails either, the chat keeps the app accent (links stay readable)
    // rather than tint an unreadable one.
    //
    // With NO page background derived (`chatSurfaceLums` empty), the accent-as-
    // text use would land on the APP theme's own surfaces, which this mapper
    // cannot measure. Rather than reach into the live theme's computed colours —
    // new coupling for one preview path — the accent is simply NOT followed then:
    // an accent-only page leaves links and the caret on the app accent, which is
    // already readable. So the accent is applied only when real chat surfaces
    // exist to check it against, and it clears AA on every one of them.
    const accentAsTextOk = chatSurfaceLums.length > 0
      && chatSurfaceLums.every((L) => contrastOn(accent.r, accent.g, accent.b, L) >= CHAT_AA_TEXT)
    if (label.ratio >= CHAT_AA_TEXT && accentAsTextOk) {
      vars['--accent'] = accentStr
      vars['--accent-fg'] = label.color
      // The hover and subtle tints must follow the page accent too, or the main
      // action reverts to the APP accent on hover (a colour from the other
      // theme, with the label's contrast broken). A small step of the accent
      // toward its own label keeps the hover distinct and `--accent-fg` readable
      // (it only moves toward that foreground). Subtle is the accent at low alpha.
      const af = label.color === WHITE ? 255 : 0
      vars['--accent-hover'] = hex(mix(accent.r, af, 0.12), mix(accent.g, af, 0.12), mix(accent.b, af, 0.12))
      vars['--accent-subtle'] = `rgba(${clampCh(accent.r)},${clampCh(accent.g)},${clampCh(accent.b)},0.2)`
    }
  }

  return vars
}
