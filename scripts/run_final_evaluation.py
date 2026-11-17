"""Legacy alias (spec harness.legacy_script_alias): python scripts/run_final_evaluation.py --input <tickets.json> --output <dir>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.harness import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
