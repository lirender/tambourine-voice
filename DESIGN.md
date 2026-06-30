# Tambourine — Design System

The source of truth for how Tambourine looks and feels. Calibrate every UI
decision against this. When a choice isn't covered here, follow the thesis.

## Thesis

Tambourine is a calm, private, local-first voice tool that lives in the macOS
menubar. It should feel like a quiet, well-made instrument — present when you
need it, invisible when you don't. **Color means status, not decoration.** The
interface is near-black and typographic; the only color you see is a signal
(you're recording, something needs you). Restraint is the brand.

**Remember-this:** the moment a red dot appears, you know you're being recorded —
and nothing else on screen competes with it.

## Typography

Already in use (`app/src/main.tsx`). Do not swap for defaults.

- **Headings:** `Instrument Serif`, weight 400. Editorial, a little human. Used
  for screen titles ("Standup — who's who?"), not for dense UI labels.
- **Body / UI:** `DM Sans`, with `-apple-system, BlinkMacSystemFont, sans-serif`
  fallback. All controls, rows, status text.
- **Never** ship system-ui/Inter/Roboto/Arial as the primary face — the
  DM Sans + Instrument Serif pairing is the typographic identity.
- **Minimum body size 14px; status/primary text 16px.** No sub-14px type.

## Color

Grayscale base + semantic-only color. Tokens mirror the Mantine `dark` palette
already defined in `main.tsx`.

| Token | Value | Use |
|-------|-------|-----|
| `bg` | `#0A0A0A` / `#000000` | window / menubar dropdown background |
| `surface` | `#111111` | Paper, Card, rows (Mantine Paper/Card default) |
| `surface-raised` | `#1A1A1A` | hovered rows, the recording banner field |
| `border` | `#2C2E33` / `#373A40` | hairlines, dividers, input outlines |
| `text` | `#C1C2C5` | primary text |
| `text-muted` | `#909296` | secondary text, transcript snippets (keep ≥4.5:1) |
| `text-faint` | `#5C5F66` | timestamps, disabled |
| **`recording`** | `#FA5252` (red) | **live recording only** — dot, banner, timer, Stop |
| **`attention`** | `#E8A33D` (amber) | needs-action: unmapped speakers, "Name speakers" badge |
| `success` | `#40C057` | transient confirmation only (rare) |

Rules:
- Red is sacred. It appears **only** when audio is actively being captured.
  Never use red for errors, links, or decoration.
- Amber is for "you have something to do" (unmapped speakers, a finished
  transcript awaiting names). Never for status that isn't actionable.
- Everything else is grayscale. No brand accent, no gradients, no purple.
  (The early meeting mockups used purple — that is explicitly rejected.)

## Layout

- **Menubar dropdown:** fixed width **380px**. The primary surface. Vertical
  stack, one strong anchor per view. Never a card mosaic.
- **Overlay window:** the small always-on-top recording dot (reused from
  dictation) doubles as the ambient "recording" affordance for meetings.
- **One job per section.** Recording banner = status+stop. "Up next" = list.
  Speaker mapping = one row per voice.
- Hairline dividers (`border`) between rows, not boxes-in-boxes. Cards only
  when the card *is* the interaction (the recording banner qualifies).

## Spacing

Mantine scale (`xs 8 / sm 12 / md 16 / lg 24 / xl 32`). Row vertical padding
`md`. Section gaps `lg`. Generous over dense — this is a glanceable surface,
not a data grid.

## Motion

Minimal and meaningful.
- **Recording pulse:** the red dot breathes (~1.5s ease-in-out). The only
  ambient animation. Signals "live" without a label.
- Dropdown open/close: native menubar behavior, no custom entrance.
- No decorative transitions, parallax, or skeleton shimmer beyond a simple
  progress state.

## Components & states (meeting feature)

From the 2026-06-29 design review (approved Variant A mockups in
`~/.gstack/projects/lirender-tambourine-voice/designs/`).

- **Recording banner:** `surface-raised` field, pulsing red dot, "Recording —
  {title}", elapsed timer, Stop. Persistent while capturing — never a toast.
- **Up next:** each meeting row shows time-until, title, and **all attendee
  names** (wrap to 2 lines; no "+N others" truncation), plus a Record affordance.
- **Speaker mapping ("who's who?"):** one row per diarized speaker, most-talkative
  first — play-sample button (aria-labelled), transcript snippet in `text-muted`,
  attendee dropdown. Primary "Save names" button, right-aligned.
- **Entry points:** on transcription complete, a "Name speakers" prompt card at
  top of the dropdown + an amber badge on the menubar icon; finished meetings
  also persist in a **Transcripts** list with a "Name speakers" action.

### Required interaction states

| State | What the user sees |
|-------|--------------------|
| Empty (no meetings) | Warm line + "Tambourine watches your calendar. Nothing scheduled." Not "No items." |
| Recording | Persistent red banner + timer + Stop |
| Transcribing | "Transcribing on GB10…" progress row |
| Done | Transcript available; amber "Name speakers" if unmapped |
| **GB10 offline** | Honest banner: "GB10 offline — transcribed locally, no speaker labels." + **Retry** when it's back |
| Partial | Some speakers mapped, some "Speaker N" placeholder |

## Accessibility

- Status is never color-only: the recording banner carries the word "Recording"
  + a timer alongside the red. Amber badges pair with text/icon.
- `text-muted` on `surface` must stay ≥4.5:1. Snippet text included.
- Touch/click targets ≥ the Mantine default; play/record buttons aria-labelled.
- Keyboard: Mantine Select/Button handle focus; preserve tab order top-to-bottom.

## Voice & copy

Utility language. Orientation, status, action — not mood or marketing.
- Good: "Recording — Standup", "Name speakers", "GB10 offline — transcribed locally".
- Bad: "Your all-in-one meeting companion", "Unlock powerful insights".
- If deleting 30% of a string still reads clearly, delete it.
