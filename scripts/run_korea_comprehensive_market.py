from __future__ import annotations

"""Run the existing Korea comprehensive-market model after src.main.

This runner intentionally does not collect any external data and does not alter
existing engine logic. It only invokes the repository's existing
src.models.korea_comprehensive_market.build_and_write() against ./output.
"""

from pathlib import Path

from src.models.korea_comprehensive_market import build_and_write


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = repo_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    result = build_and_write(output_dir)
    summary = result.get("summary") or {}

    print("[korea-comprehensive] build complete")
    print(f"[korea-comprehensive] current_score={summary.get('current_score')}")
    print(f"[korea-comprehensive] trend_score={summary.get('trend_score')}")
    print(f"[korea-comprehensive] forward_overall={summary.get('forward_overall')}")
    print(f"[korea-comprehensive] output={output_dir / 'korea_comprehensive_environment.json'}")


if __name__ == "__main__":
    main()
