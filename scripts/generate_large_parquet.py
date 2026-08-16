from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def generate(path: Path, target_mib: int, batch_rows: int = 100_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer: pq.ParquetWriter | None = None
    start = 0
    target_bytes = target_mib * 1024 * 1024
    try:
        while not path.exists() or path.stat().st_size < target_bytes:
            stop = start + batch_rows
            ids = list(range(start, stop))
            batch = pa.table(
                {
                    "id": ids,
                    "segment": [f"segment-{value % 100}" for value in ids],
                    "measure": [float((value * 17) % 10_000) / 100.0 for value in ids],
                    "flag": [value % 7 == 0 for value in ids],
                }
            )
            if writer is None:
                writer = pq.ParquetWriter(path, batch.schema, compression="zstd")
            writer.write_table(batch)
            start = stop
    finally:
        if writer is not None:
            writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic synthetic Parquet fixture by approximate file size."
    )
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--target-mib",
        type=int,
        default=1024,
        help="Approximate compressed target size in MiB (default: 1024).",
    )
    parser.add_argument("--batch-rows", type=int, default=100_000)
    args = parser.parse_args()
    if args.target_mib < 1 or args.batch_rows < 1:
        parser.error("--target-mib and --batch-rows must be positive")
    generate(args.path, args.target_mib, args.batch_rows)


if __name__ == "__main__":
    main()
