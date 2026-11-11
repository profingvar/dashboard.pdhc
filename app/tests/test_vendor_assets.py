"""Vendored front-end assets: inventory, integrity and presence.

dashboard.pdhc has no JavaScript build step and no package.json, so nothing
except `app/static/vendor/VERSIONS.md` records what the committed bundles are.
An unversioned vendored bundle cannot be audited or upgraded, so these tests
hold VERSIONS.md and the directory to each other:

  * every file the inventory lists exists on disk and is non-empty
  * every file's bytes still hash to the sha256 the inventory records
  * chartjs-plugin-zoom is present, and so is the hammer it silently needs
  * app/static/js/capability.js exists and is non-empty

Each of those is an assertion about ABSENCE being empty, and a check for
absence is the easy kind to write wrongly: a path built from the wrong root,
or a parser that returns nothing, both report a clean pass having looked at
nothing. So every such assertion here runs through a named helper, and a
CONTROL test feeds that same helper a case it MUST report. If the controls
pass, a green run is evidence the check actually looks.

Stdlib only, and no import of the Flask app: these are facts about the tree,
and they should not be able to fail for an unrelated reason.
"""

import hashlib
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VENDOR_DIR = os.path.join(ROOT, "app", "static", "vendor")
JS_DIR = os.path.join(ROOT, "app", "static", "js")
VERSIONS_MD = os.path.join(VENDOR_DIR, "VERSIONS.md")

CAPABILITY_JS = os.path.join(JS_DIR, "capability.js")

# The inventory is the source of truth for what is vendored; these are the two
# files this change added, named explicitly so that deleting a row from
# VERSIONS.md cannot quietly shrink what is checked.
ZOOM_PLUGIN = "chartjs-plugin-zoom.umd.min.js"
HAMMER = "hammer.min.js"

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$")
_SHA256 = re.compile(r"`([0-9a-f]{64})`")


# --------------------------------------------------------------------------
# helpers — the real tests and the controls both go through these
# --------------------------------------------------------------------------

def parse_inventory(markdown):
    """Return [(filename, sha256)] from the '## Inventory' table.

    Parses only the Inventory section, so the '## Bundled sub-dependencies'
    table below it (date-fns, which is not a file) is not mistaken for one.
    """
    entries = []
    in_section = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Inventory"
            continue
        if not in_section:
            continue
        m = _ROW.match(line.strip())
        if not m:
            continue
        name, rest = m.group(1), m.group(2)
        shas = _SHA256.findall(rest)
        entries.append((name, shas[-1] if shas else None))
    return entries


def missing_files(names, directory=VENDOR_DIR):
    """Return the subset of `names` that is not a non-empty file in `directory`.

    Empty counts as missing: a zero-byte bundle is a failed download, and it
    would load without error and do nothing.
    """
    absent = []
    for name in names:
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            absent.append(name)
    return absent


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_mismatches(entries, directory=VENDOR_DIR):
    """Return [(name, recorded, actual)] for files whose bytes moved."""
    bad = []
    for name, recorded in entries:
        path = os.path.join(directory, name)
        if recorded is None or not os.path.isfile(path):
            continue
        actual = sha256_of(path)
        if actual != recorded:
            bad.append((name, recorded, actual))
    return bad


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------

def test_versions_md_exists():
    assert os.path.isfile(VERSIONS_MD), (
        "app/static/vendor/VERSIONS.md is missing. Without it nothing records "
        "what version each vendored bundle is."
    )


def test_inventory_parses_and_is_not_empty():
    entries = parse_inventory(open(VERSIONS_MD, encoding="utf-8").read())
    # Guards the parser itself: an inventory that parses to nothing would make
    # every per-file test below vacuously true.
    assert entries, "VERSIONS.md '## Inventory' table parsed to zero rows"
    assert len(entries) >= 4, "expected at least 4 vendored bundles, got %d" % len(entries)


def test_every_inventoried_file_exists_and_is_non_empty():
    entries = parse_inventory(open(VERSIONS_MD, encoding="utf-8").read())
    names = [name for name, _ in entries]
    assert missing_files(names) == [], (
        "VERSIONS.md lists files that are not on disk: %s" % missing_files(names)
    )


def test_every_vendored_js_file_is_inventoried():
    """The other direction: a bundle added without a VERSIONS.md row."""
    entries = parse_inventory(open(VERSIONS_MD, encoding="utf-8").read())
    listed = {name for name, _ in entries}
    on_disk = {f for f in os.listdir(VENDOR_DIR) if f.endswith(".js")}
    undocumented = sorted(on_disk - listed)
    assert undocumented == [], (
        "vendored but not listed in VERSIONS.md: %s — an unversioned bundle "
        "cannot be audited or upgraded" % undocumented
    )


def test_every_inventoried_file_matches_its_recorded_sha256():
    entries = parse_inventory(open(VERSIONS_MD, encoding="utf-8").read())
    assert all(sha is not None for _, sha in entries), (
        "every Inventory row must record a sha256: %s"
        % [n for n, s in entries if s is None]
    )
    assert digest_mismatches(entries) == [], (
        "bundle bytes no longer match VERSIONS.md: %s" % digest_mismatches(entries)
    )


# --------------------------------------------------------------------------
# the zoom plugin and the hammer it needs
# --------------------------------------------------------------------------

def test_zoom_plugin_is_vendored():
    assert missing_files([ZOOM_PLUGIN]) == [], (
        "%s is missing — it provides zoom, pan and every touch gesture in the "
        "chart, including pinch-to-zoom on an iPad" % ZOOM_PLUGIN
    )


def test_zoom_plugin_is_inventoried():
    listed = {name for name, _ in parse_inventory(open(VERSIONS_MD, encoding="utf-8").read())}
    assert ZOOM_PLUGIN in listed


def test_hammer_is_vendored_because_the_plugin_does_not_bundle_it():
    """The plugin reads a GLOBAL Hammer; without it, touch gestures are silent.

    This asserts the premise as well as the file, because the premise is the
    whole reason hammer is here. chartjs-plugin-zoom's UMD wrapper resolves
    `t.Hammer` from the global object and its setup is guarded by `n && ...`,
    so a missing Hammer throws nothing and logs nothing — pinch-to-zoom and
    touch panning just never happen, while desktop wheel-zoom keeps working.
    If a future upgrade bundles hammer, this test should fail and be rewritten,
    not deleted.
    """
    plugin = open(os.path.join(VENDOR_DIR, ZOOM_PLUGIN), encoding="utf-8").read()
    assert "Hammer" in plugin, (
        "%s no longer references a global Hammer — re-check whether hammer is "
        "still required before changing this test" % ZOOM_PLUGIN
    )
    assert missing_files([HAMMER]) == [], (
        "%s is vendored but %s is not; the plugin's touch support would "
        "silently do nothing" % (ZOOM_PLUGIN, HAMMER)
    )


# --------------------------------------------------------------------------
# capability helper
# --------------------------------------------------------------------------

def test_capability_js_exists_and_is_non_empty():
    assert missing_files(["capability.js"], directory=JS_DIR) == [], (
        "app/static/js/capability.js is missing or empty"
    )


def test_capability_js_uses_the_media_query_and_not_the_user_agent():
    src = open(CAPABILITY_JS, encoding="utf-8").read()
    assert "(hover: hover) and (pointer: fine)" in src, (
        "capability.js must derive hover from matchMedia, not from a device guess"
    )
    # A user agent string cannot report whether a trackpad is attached right
    # now, which is the whole point of the helper.
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith(("*", "/*", "//"))
    )
    assert "userAgent" not in code, "capability.js must never sniff the user agent"


# --------------------------------------------------------------------------
# CONTROLS — these make a green run above mean something
# --------------------------------------------------------------------------

def test_control_a_nonexistent_vendor_file_is_reported_missing(tmp_path):
    """The same helper the real tests use must report a file that is not there.

    Without this, `missing_files(...) == []` could be passing because the
    helper looks in the wrong directory and finds nothing to complain about.

    The mixed-list half runs against tmp_path rather than the real vendor
    directory on purpose: a control must fail only for its own reason. An
    earlier version listed real bundle names here, so removing a bundle made
    this control fail too — which is noise that disguises which check caught
    the regression.
    """
    bogus = "definitely-not-vendored-xyzzy.umd.min.js"
    assert not os.path.exists(os.path.join(VENDOR_DIR, bogus)), (
        "the control file must genuinely not exist"
    )
    assert missing_files([bogus]) == [bogus]

    # It must still single out the absent one when mixed with present ones.
    (tmp_path / "present-a.min.js").write_bytes(b"a")
    (tmp_path / "present-b.min.js").write_bytes(b"b")
    assert missing_files(
        ["present-a.min.js", bogus, "present-b.min.js"], directory=str(tmp_path)
    ) == [bogus]


def test_control_an_empty_file_is_reported_missing(tmp_path):
    empty = tmp_path / "empty.min.js"
    empty.write_bytes(b"")
    nonempty = tmp_path / "real.min.js"
    nonempty.write_bytes(b"x")
    assert missing_files(["empty.min.js", "real.min.js"], directory=str(tmp_path)) == [
        "empty.min.js"
    ]


def test_control_a_changed_byte_is_reported_as_a_digest_mismatch(tmp_path):
    """The digest check must notice bytes that moved."""
    f = tmp_path / "sample.min.js"
    f.write_bytes(b"console.log(1)")
    good = sha256_of(str(f))
    assert digest_mismatches([("sample.min.js", good)], directory=str(tmp_path)) == []
    f.write_bytes(b"console.log(2)")
    bad = digest_mismatches([("sample.min.js", good)], directory=str(tmp_path))
    assert len(bad) == 1 and bad[0][0] == "sample.min.js"


def test_control_the_inventory_parser_reads_names_and_skips_other_tables():
    """A parser that returned [] would make the per-file tests vacuous."""
    sample = "\n".join([
        "## Inventory",
        "",
        "| File | Version | Source URL | sha256 |",
        "|---|---|---|---|",
        "| `a.min.js` | 1.0.0 | https://example.invalid/a | `%s` |" % ("a" * 64),
        "",
        "## Bundled sub-dependencies",
        "",
        "| Inside | Library | Version |",
        "|---|---|---|",
        "| `a.min.js` | `date-fns` | unestablished |",
    ])
    assert parse_inventory(sample) == [("a.min.js", "a" * 64)]
    # ...and a document with no Inventory section yields nothing, rather than
    # silently scraping whatever backticks it finds.
    assert parse_inventory("## Something else\n\n| `b.min.js` | x |") == []
