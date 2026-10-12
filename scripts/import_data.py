"""Copy the original data files into the repository's expected location."""

import argparse
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path, help="Directory containing the Colab and its data")
args = parser.parse_args()
destination = Path(__file__).resolve().parents[1] / "data" / "raw"
destination.mkdir(parents=True, exist_ok=True)
for name in ["MIEM_knowledge_base_unified.zip", "HSE_Answers_RU.xlsx"]:
    source = args.source / name
    if not source.is_file():
        print(f"Missing: {source}")
        continue
    if source.stat().st_size >= 100 * 1024 * 1024:
        raise ValueError(f"{name} exceeds GitHub's 100 MiB file limit; use server storage")
    if source.resolve() != (destination / name).resolve():
        shutil.copy2(source, destination / name)
    print(f"Imported: {name}")
