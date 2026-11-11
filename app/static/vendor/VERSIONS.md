# Vendored front-end bundles

`dashboard.pdhc` has **no JavaScript build step and no `package.json`**. Every
third-party library is committed here as a UMD bundle and loaded with a plain
`<script>` tag. That is deliberate (#471), and it has one cost: nothing records
what version a bundle is, so nothing can audit or upgrade it. This file is that
record.

**Rule: a file added to this directory is added to the Inventory table in the
same change.** `app/tests/test_vendor_assets.py` enforces both halves — every
row must exist on disk, and every row's sha256 must match the bytes on disk.

## Inventory

| File | Version | Source URL | sha256 |
|---|---|---|---|
| `chart.umd.min.js` | 4.4.6 | https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js | `9653a0813db743bbe78332a3896e28c7bc7546e4fff51e7e979e908d1f0471d1` |
| `chartjs-adapter-date-fns.bundle.min.js` | 3.0.0 | https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js | `ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615` |
| `chartjs-plugin-zoom.umd.min.js` | 2.2.0 | https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-zoom/2.2.0/chartjs-plugin-zoom.min.js | `e4a088e5bab93be6ee47c939eeb9ebaa80e0b39156d4bdfd1af9c844be81b6c4` |
| `hammer.min.js` | 2.0.8 (distribution) / `2.0.7` (banner — see note) | https://cdnjs.cloudflare.com/ajax/libs/hammer.js/2.0.8/hammer.min.js | `7953631f0e54794d2352a3cfa591c0914d73e14f90141058e3cf16bee7939bcf` |

## Bundled sub-dependencies

Not files of their own, so not in the Inventory table above and not checked by
the test.

| Inside | Library | Version |
|---|---|---|
| `chartjs-adapter-date-fns.bundle.min.js` | `date-fns` | **unestablished** — see note 4 |

## How each version was established

Versions here are **measured**, not assumed. Where a version could not be
established from the bytes it says **unestablished** rather than a guess.

1. **`chart.umd.min.js` → 4.4.6.** Two independent pieces of evidence agree.
   Its own banner carries `Chart.js v4.4.6` *and* a jsDelivr provenance line,
   `Original file: /npm/chart.js@4.4.6/dist/chart.umd.js`. Independently, the
   file is **byte-identical** (sha256 above) to the current
   `cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js`. Byte-identity is
   the stronger evidence: a banner is a string anyone can edit.

2. **`chartjs-adapter-date-fns.bundle.min.js` → 3.0.0.** Banner says
   `chartjs-adapter-date-fns v3.0.0`; it carries no provenance line, so the
   version was confirmed the other way — the file is **byte-identical** to
   `cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js`.

3. **`hammer.min.js` → 2.0.8, with its banner reading `v2.0.7`.** This is a
   known upstream quirk, not a bad download: hammerjs 2.0.8 shipped a `dist`
   that was never rebuilt, so the published 2.0.8 artifact still carries the
   2.0.7 banner. **Do not "correct" the row to 2.0.7.** The evidence that this
   is the 2.0.8 artifact is external to the file, and there are two independent
   sources: its sha512 matches cdnjs's published SRI for
   `hammer.js/2.0.8/hammer.min.js`
   (`sha512-UXumZrZNiOwnTcZSHLOfcTs0aos2MzBWHXOHOuB0J/R44QB0dwY5JgfbvljXcklVf65Gc4El6RjZ+lnwd2az2g==`),
   and it is byte-identical to `cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js`.
   Read from the file alone, this version is unestablished.

4. **`date-fns` bundled inside the adapter → unestablished.** The bundle carries
   no `date-fns` version banner; the only internal trace is a 2.x-era runtime
   warning string, which fixes the major and nothing more. npm metadata for
   `chartjs-adapter-date-fns@3.0.0` pins `date-fns 2.19` as the devDependency the
   `.bundle.` artifact is built against — that is corroboration about the
   *package*, not a measurement of *these bytes*, so the row stays
   **unestablished**.

5. **`chartjs-plugin-zoom.umd.min.js` → 2.2.0** (added here; see below).

## chartjs-plugin-zoom 2.2.0 — why this version, and why hammer came with it

**Compatibility.** `chartjs-plugin-zoom@2.2.0` declares
`peerDependencies: {"chart.js": ">=3.2.0"}`, which the vendored Chart.js 4.4.6
satisfies. 2.2.0 is also the latest published version, so nothing newer was
passed over.

**Integrity.** Downloaded from cdnjs; its sha512 matches cdnjs's published SRI
(`sha512-FRGbE3pigbYamZnw4+uT4t63+QJOfg4MXSgzPn2t8AWg9ofmFvZ/0Z37ZpCawjfXLBSVX2p2CncsmUH2hzsgJg==`)
and its bytes are identical to `npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js`.
The local filename differs from the upstream one (`.umd.min.js` vs `.min.js`) to
match the naming already used in this directory; the bytes are unchanged.

**It does NOT bundle hammerjs, and that is why `hammer.min.js` is here too.**
This was checked in the bundle rather than assumed. Its UMD wrapper takes hammer
as an external:

```
(t=...globalThis...||self).ChartZoom = n(t.Chart, t.Hammer, t.Chart.helpers)
```

— so the second argument is read from the **global `Hammer`** at script-load
time. It is used unguarded at the point of use, `new n.Manager(canvas)`, to
register `Pinch` and `Pan` recognisers.

**The failure mode without hammer is silence, not an error.** The plugin's
`start` hook calls that setup as `n && $(t, a)` — i.e. it is skipped entirely
when `Hammer` is undefined. So a dashboard shipped without hammer would throw
nothing, log nothing, and report no failure; **pinch-to-zoom and single-finger
touch panning would simply never happen on an iPad**, while wheel-zoom and
mouse-drag-zoom (which are plain DOM listeners, not hammer) kept working on a
desktop. That asymmetry is exactly how such a gap survives a desktop test pass.

## Load order — hammer BEFORE the plugin

The plugin resolves `t.Hammer` when its own script executes, not lazily at first
gesture. A correct order is therefore:

```html
<script src="{{ url_for('static', filename='vendor/chart.umd.min.js') }}"></script>
<script src="{{ url_for('static', filename='vendor/chartjs-adapter-date-fns.bundle.min.js') }}"></script>
<script src="{{ url_for('static', filename='vendor/hammer.min.js') }}"></script>
<script src="{{ url_for('static', filename='vendor/chartjs-plugin-zoom.umd.min.js') }}"></script>
```

Loading hammer *after* the plugin yields the silent no-gesture state described
above, because by then the plugin has already captured `undefined`.

**These files are not yet referenced by any template.** `app/templates/base.html`
loads only Chart.js and the date-fns adapter; wiring the two new bundles in (and
calling `Chart.register(ChartZoom)`) is a separate change and was deliberately
not made here.

## Refreshing this file

```
shasum -a 256 app/static/vendor/*.js
```

Compare against the Inventory column; `app/tests/test_vendor_assets.py` does the
same comparison automatically.
