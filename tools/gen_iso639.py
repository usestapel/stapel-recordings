#!/usr/bin/env python3
"""Regenerate iso639.py (ISO 639-2/3 -> ISO 639-1 mapping).

Primary source (preferred): the Library of Congress ISO 639-2 registry,
    https://www.loc.gov/standards/iso639-2/ISO-639-2_utf-8.txt
which is a pipe-delimited file with the columns:
    alpha-3 (bibliographic) | alpha-3 (terminological) | alpha-2 | English name | French name

As of 2026-09-02 that URL returns HTTP 403 from this machine (Cloudflare
"Just a moment..." JS challenge page, confirmed with both `curl` using a
browser User-Agent and Anthropic's WebFetch tool) -- LOC now fronts static
files with a bot-blocking challenge that a plain HTTP client cannot pass.

Fallback source (used to build the checked-in iso639.py): the `pycountry`
package (https://pypi.org/project/pycountry/), specifically its bundled
`databases/iso639-3.json`, which is itself derived from the SIL ISO 639-3
registry merged with the ISO 639-2 alpha-2/bibliographic data and is the
same data Debian's `iso-codes` package ships. Each record looks like:
    {"alpha_3": "deu", "alpha_2": "de", "bibliographic": "ger", "name": "German", ...}
Only records that carry an "alpha_2" field have an ISO 639-1 equivalent;
of those, the ~20 that also carry "bibliographic" contribute a second
3-letter key (the older bibliographic code) mapping to the same 2-letter
code.

Usage:
    python3 gen_iso639.py [output_path]

Behavior:
    1. Try to fetch and parse the LOC pipe-delimited file.
    2. If that fails (network error, non-200, or a Cloudflare challenge
       page instead of pipe-delimited data), fall back to `pycountry`,
       installing it into a throwaway venv-local pip cache if not already
       importable (never touches any project venv).
    3. Emit a black-compatible `ISO639_2_TO_1: dict[str, str]` literal to
       `output_path` (default: iso639.py next to this script).
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOC_URL = "https://www.loc.gov/standards/iso639-2/ISO-639-2_utf-8.txt"
HERE = Path(__file__).resolve().parent


def fetch_loc_table() -> str | None:
    """Try to download the LOC pipe-delimited ISO 639-2 table.

    Returns the raw text on success, or None if unreachable / blocked.
    """
    req = urllib.request.Request(
        LOC_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return None
            text = resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None

    # A Cloudflare challenge page (or any non-table response) won't contain
    # pipe-delimited rows with 5 fields; sanity-check the first data line.
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    if first_line.count("|") < 4:
        return None
    return text


def parse_loc_table(text: str) -> dict[str, str]:
    """Parse the LOC pipe-delimited format into {alpha3: alpha2}."""
    mapping: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        bib, term, alpha2 = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not alpha2:
            continue
        alpha2 = alpha2.lower()
        if bib:
            mapping[bib.lower()] = alpha2
        if term:
            mapping[term.lower()] = alpha2
    return mapping


def build_via_pycountry() -> tuple[dict[str, str], str]:
    """Build {alpha3: alpha2} using pycountry; install it locally if needed.

    Returns (mapping, provenance_note).
    """
    try:
        import pycountry  # type: ignore
    except ImportError:
        venv_dir = HERE / ".venv-iso"
        if not venv_dir.exists():
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)], check=True
            )
        pip = venv_dir / "bin" / "pip"
        subprocess.run(
            [str(pip), "install", "-q", "pycountry"], check=True
        )
        py = venv_dir / "bin" / "python"
        # Re-exec this build step inside the venv, since pycountry is not
        # importable in the current interpreter.
        result = subprocess.run(
            [str(py), __file__, "--pycountry-only", str(HERE / "iso639.py")],
            check=True,
        )
        sys.exit(result.returncode)

    mapping: dict[str, str] = {}
    for lang in pycountry.languages:
        alpha2 = getattr(lang, "alpha_2", None)
        alpha3 = getattr(lang, "alpha_3", None)
        bib = getattr(lang, "bibliographic", None)
        if not alpha2 or not alpha3:
            continue
        mapping[alpha3.lower()] = alpha2.lower()
        if bib:
            mapping[bib.lower()] = alpha2.lower()

    note = f"pycountry {pycountry.__version__} (databases/iso639-3.json)"
    return mapping, note


def render_module(mapping: dict[str, str], source_note: str) -> str:
    today = datetime.date.today().isoformat()
    keys = sorted(mapping)

    lines: list[str] = []
    lines.append('"""ISO 639-2 / ISO 639-3 (3-letter) -> ISO 639-1 (2-letter) language code map.')
    lines.append("")
    lines.append("Source of truth (preferred, per the project's fetch order):")
    lines.append(f"    {LOC_URL}")
    lines.append("Fetched/regenerated: 2026-09-02 (this file); attempted again: " + today + ".")
    lines.append("")
    lines.append("That LOC endpoint returned HTTP 403 (Cloudflare JS challenge) on every")
    lines.append("attempt from this machine, so this table was actually built from:")
    lines.append(f"    {source_note}")
    lines.append("which packages the same ISO 639-2/639-3 <-> 639-1 relationships (it is")
    lines.append("the data source used by Debian's iso-codes and langcodes as well).")
    lines.append("")
    lines.append("Both the bibliographic and terminological 3-letter codes map to the")
    lines.append("2-letter code for the ~20 languages where those differ, e.g.:")
    lines.append("    ger -> de   and   deu -> de")
    lines.append("    fre -> fr   and   fra -> fr")
    lines.append("    chi -> zh   and   zho -> zh")
    lines.append("    dut -> nl   and   nld -> nl")
    lines.append("")
    lines.append("Only codes that have an ISO 639-1 equivalent are included (most ISO")
    lines.append("639-2/3 codes do not; those are intentionally omitted).")
    lines.append("")
    lines.append("Regenerate with gen_iso639.py.")
    lines.append('"""')
    lines.append("")
    lines.append("ISO639_2_TO_1: dict[str, str] = {")

    # A few pairs per line, black-compatible, max line length 88.
    line_buf = "    "
    for key in keys:
        pair = f'"{key}": "{mapping[key]}", '
        if len(line_buf) + len(pair) > 88:
            lines.append(line_buf.rstrip())
            line_buf = "    "
        line_buf += pair
    if line_buf.strip():
        lines.append(line_buf.rstrip())

    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = sys.argv[1:]
    pycountry_only = "--pycountry-only" in args
    if pycountry_only:
        args = [a for a in args if a != "--pycountry-only"]
    out_path = Path(args[0]) if args else HERE / "iso639.py"

    if pycountry_only:
        mapping, note = build_via_pycountry()
    else:
        text = fetch_loc_table()
        if text is not None:
            mapping = parse_loc_table(text)
            note = LOC_URL
            (HERE / "ISO-639-2_utf-8.txt").write_text(text, encoding="utf-8")
        else:
            print(
                f"WARNING: {LOC_URL} unreachable/blocked; falling back to pycountry.",
                file=sys.stderr,
            )
            mapping, note = build_via_pycountry()

    module_src = render_module(mapping, note)
    out_path.write_text(module_src, encoding="utf-8")
    print(f"Wrote {len(mapping)} entries to {out_path}")


if __name__ == "__main__":
    main()
