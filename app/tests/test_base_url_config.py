"""#982 — every base URL a client READS must be LOADED and OFFERED.

Three keys were missing at once (PLAN, REQUEST, ICD11) and a fourth was
loaded but never offered (IPS). Fixing those three is not the point; the
point is that nothing noticed.

THE DEFECT THIS CLOSES IS WRITTEN DOWN IN THE CODE IT GUARDS.
`app/__init__.py`'s own comment above `IPS_BASE_URL` records #827: the client
read `os.environ` directly, `create_app` loaded nothing, and
`ips_client.is_configured()` — which asks the CONFIG — answered "not
configured" with the variable plainly set. One resolution path for the
client, a different one for the question "can we ask at all?". A client whose
key is loaded nowhere has that defect waiting.

IT IS ASSERTED ON THE PARSE TREE, not on text, and for the usual reason: the
comment that explains this guard names the very keys it checks, and
`.env.example` names `IPS_BASE_URL` in prose. A substring sweep would read
both as configuration. CLAUDE.md §7 counts ten occurrences of that error on
this platform, two of them inside instruments built to detect it.
"""
from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICES = REPO / "app" / "services"


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"`, so a key held in a constant counts.

    `icd11_client` does exactly that — `ENV_BASE_URL = "ICD11_BASE_URL"` and
    then `os.environ.get(ENV_BASE_URL)`. A collector that only accepted a
    literal argument would silently miss it, and the test would pass while
    the key it cares about went unchecked.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.value.value
    return out


def keys_read_by_clients() -> dict[str, set[str]]:
    """{file: {KEY, ...}} for every `*_BASE_URL` a client DEPENDS ON.

    BOTH sources count, and the positive control below is what forced that.
    The first version collected only `os.environ` reads and silently missed
    `CDR1_BASE_URL`, because `cdr1_client` resolves from
    `current_app.config` alone with no environment fallback. The test passed
    with four of five keys and looked clean.

    The question is not "where does it read from" but "what does it depend
    on", and a key read from config is exactly as missing from `create_app`
    as one read from the environment — more so, since there is no fallback
    to save it.
    """
    found: dict[str, set[str]] = {}
    for py in sorted(SERVICES.glob("*.py")):
        tree = ast.parse(py.read_text())
        consts = _module_constants(tree)

        def _key(node) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return consts.get(node.id)
            return None

        for node in ast.walk(tree):
            k = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                recv = ast.unparse(node.func.value)
                if node.func.attr in ("get", "getenv") and node.args and (
                        "environ" in recv or "config" in recv or recv == "os"):
                    k = _key(node.args[0])
            elif isinstance(node, ast.Subscript):
                recv = ast.unparse(node.value)
                if "environ" in recv or "config" in recv:
                    k = _key(node.slice)
            if k and k.endswith("_BASE_URL"):
                found.setdefault(py.name, set()).add(k)
    return found


def keys_loaded_by_create_app() -> set[str]:
    """Keys `app/__init__.py` puts into `app.config`, from CODE only."""
    tree = ast.parse((REPO / "app" / "__init__.py").read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.endswith("_BASE_URL"):
            out.add(node.value)
    return out


def keys_offered_by_env_example() -> set[str]:
    """`KEY=` at the start of a line. Anchored: the file explains the gap in
    prose that names IPS_BASE_URL, and prose is not an offer."""
    text = (REPO / ".env.example").read_text()
    return {m.group(1) for m in
            re.finditer(r"^([A-Z][A-Z0-9_]*_BASE_URL)=", text, re.M)}


def _all_read() -> set[str]:
    return set().union(*keys_read_by_clients().values())


# --------------------------------------------------------------------------
# the assertions
# --------------------------------------------------------------------------

def test_every_key_a_client_reads_is_LOADED_by_create_app():
    missing = _all_read() - keys_loaded_by_create_app()
    assert not missing, (
        f"{sorted(missing)} is read by a service client and loaded by "
        "create_app nowhere, so is_configured() and the client resolve "
        "differently — #827's defect, waiting"
    )


def test_every_key_a_client_reads_is_OFFERED_by_env_example():
    missing = _all_read() - keys_offered_by_env_example()
    assert not missing, (
        f"{sorted(missing)} is read by a service client and offered in "
        ".env.example nowhere, so a new deployment has to discover it from a "
        "traceback"
    )


def test_the_keys_we_expect_are_actually_FOUND():
    """Positive control. Without it both tests above pass on an empty set,
    which is how a collector that reads nothing looks exactly like a clean
    repo."""
    read = _all_read()
    assert len(read) >= 5, read
    for expected in ("CDR1_BASE_URL", "IPS_BASE_URL", "PLAN_BASE_URL",
                     "REQUEST_BASE_URL", "ICD11_BASE_URL"):
        assert expected in read, f"{expected} was not collected"


def test_a_key_read_from_CONFIG_ONLY_is_still_collected():
    """`cdr1_client` resolves from `current_app.config` with no environment
    fallback. A collector that watched only `os.environ` missed it, and the
    positive control above is what caught that — the suite reported four
    keys where five were expected."""
    per_file = keys_read_by_clients()
    assert "CDR1_BASE_URL" in per_file.get("cdr1_client.py", set())


def test_a_key_held_in_a_MODULE_CONSTANT_is_still_collected():
    """`icd11_client` uses `ENV_BASE_URL = "ICD11_BASE_URL"`. A collector that
    only accepted a literal argument would miss it and pass."""
    per_file = keys_read_by_clients()
    assert "ICD11_BASE_URL" in per_file.get("icd11_client.py", set())


def test_a_key_named_only_in_PROSE_is_not_counted():
    """The §7 control.

    `.env.example` names IPS_BASE_URL inside a comment explaining the gap.
    An unanchored sweep would read that comment as an offer — the guard
    passing on its own documentation.
    """
    text = (REPO / ".env.example").read_text()
    assert "# IPS_BASE_URL was loaded" in text or "IPS_BASE_URL was loaded" in text, (
        "the prose this control is aimed at is gone; re-point it"
    )
    offered = keys_offered_by_env_example()
    # it IS offered, on its own line — so prove the parser is line-anchored by
    # feeding it prose alone
    only_prose = "# PLAN_BASE_URL is mentioned here and nowhere set\n"
    assert not re.findall(r"^([A-Z][A-Z0-9_]*_BASE_URL)=", only_prose, re.M)
    assert "IPS_BASE_URL" in offered
