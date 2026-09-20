# Cairn Brand Brief — handoff to Claude Design

Create the brand identity for **cairn** (always lowercase), an open-source CLI tool.

## What it is (one paragraph)

Cairn is local-first shared memory for teams of AI coding agents. Agents store
and retrieve memories through six verbs (store, retrieve, list, get, archive,
purge); vaults sync peer-to-peer over git. No accounts, no API keys, no cloud —
it runs entirely on the user's machine on SQLite + local embeddings. Its
signature feature is the "Memory Galaxy": memories rendered as a navigable 3D
starfield where each team's memories form its own star cluster. Core belief:
memories are data, not instructions.

## The name

A cairn is a stack of stones hikers build to mark a trail — small, humble,
human-scale markers that guide strangers through wilderness. That IS the
product: small memory-stones, stacked by many hands, guiding agents that come
after. Lean into this metaphor hard: stacked/balanced stones, trail markers,
wayfinding, wilderness, cairns on a ridge at dusk.

## Personality

Quiet, sturdy, trustworthy, a little wild. Trail-guide energy, not
Silicon-Valley-AI energy. Think national-park signage meets terminal aesthetic.
Warm stone + deep space: the tension between earth (local, grounded, stone) and
sky (galaxy, embeddings, constellations) is the visual core.

## Deliverables

1. **Primary mark** — stacked-stones motif that also reads as constellation /
   nodes-and-edges at small sizes. Must work at 16px favicon AND as a hero.
2. **Wordmark** — "cairn" lowercase, monospace-adjacent or a grotesque with
   terminal character. Pairing with the mark (horizontal lockup).
3. **Color palette** — dark-first (the product lives in terminals and a dark
   3D galaxy UI): stone warm grays, one ember/amber accent (trail-marker
   warmth), one deep-space background, one starlight text tone. Provide hex +
   usage ratios. Must include a pure-monochrome (1-bit) version of the mark
   for terminal/ASCII contexts.
4. **Favicon + galaxy loading mark** — tiny-size survival test.
5. **ASCII banner** — a `cairn` text banner for CLI startup output (pure
   printable ASCII, max ~10 lines, works in any terminal).
6. **README hero** — wide composition: stone cairn silhouette under/inside a
   starfield, space left for the tagline.
7. **OG/social card** (1200x630) reusing the hero system.

## Formats

SVG masters for everything vector; PNG exports at 1x/2x for favicon sizes;
palette as CSS variables + JSON. All work MIT-licensed, no stock, no fonts
requiring paid licenses (system fonts, SIL OFL, or hand-drawn lettering only).

## Avoid

Generic-AI clichés (glowing brains, circuit heads, sparkles, gradients-on-
black SaaS blobs), corporate blues, anything that needs color to be
recognizable (it must survive as terminal ASCII), anything resembling AWS
orange (the product is deliberately post-cloud).
