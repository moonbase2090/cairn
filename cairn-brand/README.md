# cairn — brand packet

Local-first shared memory for teams of AI coding agents.
All assets MPL-2.0 licensed. Type: IBM Plex Mono + IBM Plex Sans (SIL OFL).

Open `brand-sheet.html` in a browser for the full system (mark, wordmark,
palette, favicon tests, ASCII banner, README hero, OG card).

## Contents

    brand-sheet.html          full brand sheet, self-contained
    assets/cairn-mark.svg     primary mark, full color
    assets/cairn-mark-mono.svg 1-bit mark (currentColor)
    assets/cairn-mark-16.svg  3-stone simplification for <= 20px
    assets/cairn-lockup.svg   horizontal lockup, mark + wordmark
    assets/favicon-16/32/64/128.png   dark-background favicons
    assets/mark-256/512.png   transparent PNG marks
    assets/palette.css        CSS custom properties
    assets/palette.json       palette + usage notes
    assets/ascii-banner.txt   CLI startup banner (9 lines, 7-bit ASCII)

## Mark

Four flat, hand-stacked stones and an ember top marker, joined by a trail line
that doubles as an edge between nodes. Clearspace = the base stone's half-width
on all sides. Minimum size 16px, where the stack drops to three stones and the
trail line is removed. Never rotate, re-stack, or recolor stones individually.

## Palette

    void        #0A0C11   58%   deep-space background, terminal, galaxy canvas
    basalt      #151820   20%   surfaces, cards, code blocks
    ridge       #232730         hairlines, borders, inactive strokes
    stone-700   #3C3730   12%   shadowed stone, dim text on light
    stone-500   #6B6259         secondary text on dark, metadata
    stone-300   #A79C90         mark fills, body text on dark
    stone-100   #D6CDC2         top stones, terminal text, highlights
    starlight   #F4F0E9   7%    primary text, wordmark, stars
    ember       #D96A3C   3%    trail marker, active verb, links
    ember-glow  #F0A468         hover and glow tint only, never body text

Ember never exceeds ~3% of a surface: it marks the trail, it is not the trail.

## Type

    wordmark    IBM Plex Mono 600, tracking -4.5%, always lowercase
    ui / prose  IBM Plex Sans 400/600, sentence case
    terminal    IBM Plex Mono 400, ember reserved for the active verb
