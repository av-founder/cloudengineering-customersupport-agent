"""CI gate (FR-09): python -m scripts.verify_kb_manifest [--write] [--kb-path PATH]

Fails the build when the knowledge-base ingestion path holds anything other than the 29 manifest-locked official articles
(e.g. development_tickets.json or a private agent snippet file). Use --write only after Ines Varga approves a documentation change.
"""
import argparse
import json
import sys
from pathlib import Path

from src.config import get_settings
from src.retrieve import KBContaminationError, build_manifest, load_official_docs


# Input:  argv (list[str] | None) – command-line arguments
# What:   Verifies (default) or regenerates (--write) kb_manifest.lock for the official documentation file
# Output: int – process exit code (0 = clean, 1 = FR-09 contamination)
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="regenerate kb_manifest.lock from the approved documentation file")
    ap.add_argument("--kb-path", type=Path, default=None, help="knowledge-base file to verify (defaults to KB_PATH)")
    args, s = ap.parse_args(argv), get_settings()
    kb_path = (args.kb_path or s.kb_path).resolve()
    if args.write:
        s.kb_manifest_path.write_text(json.dumps(build_manifest(kb_path), indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"Wrote {s.kb_manifest_path.name} for {kb_path.name}")
        return 0
    try:
        docs = load_official_docs(kb_path, s.kb_manifest_path)
    except KBContaminationError as e:
        print(f"FATAL {e}", file=sys.stderr)
        return 1
    print(f"OK: {len(docs)} official articles match {s.kb_manifest_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
