"""
SQLite storage backend for fire danger index data.

Stores hourly point data in SQLite databases (one per year-month) using
BLOB columns for compact float32 array storage. This is ~3× smaller
than CSV and ~10× faster to read.

Schema per database:
    points: point_id, latitude, longitude
    data:   variable TEXT, timestamp TEXT, values BLOB (float32 array)

The BLOB stores raw bytes of a numpy float32 array (28K points × 4 bytes
= ~112KB per row), indexed by (variable, timestamp) for fast lookups.

Also generates binary web files (.bin) for fast browser loading:
    Header: uint32 n_points, uint32 n_hours
    Body: n_hours × n_points float32 values (contiguous)
"""

import logging
import sqlite3
import struct
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class SQLiteStorage:
    """Write and read fire danger data using SQLite."""

    def __init__(self, output_base, year, month):
        self.output_base = Path(output_base)
        self.year = year
        self.month = month
        self.month_dir = self.output_base / str(year) / f"{month:02d}"
        self.db_path = self.month_dir / f"data_{year}_{month:02d}.db"
        self._conn = None
        self._accumulator = {}  # variable → [(timestamp_str, float32_array)]

    def open(self):
        """Open/create the database."""
        self.month_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS points (
                point_id INTEGER PRIMARY KEY,
                latitude REAL,
                longitude REAL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS data (
                variable TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                values_blob BLOB NOT NULL,
                PRIMARY KEY (variable, timestamp)
            )
        """)
        self._conn.commit()
        return self

    def store_points(self, points_df):
        """Store the point index."""
        rows = [(int(r.point_id), float(r.latitude), float(r.longitude))
                for _, r in points_df.iterrows()]
        self._conn.executemany(
            "INSERT OR REPLACE INTO points VALUES (?, ?, ?)", rows
        )
        self._conn.commit()

    def add(self, variable, timestamp, values):
        """Accumulate one hour of data for a variable."""
        ts_str = timestamp.strftime("%Y-%m-%d %H:%M")
        arr = np.asarray(values, dtype=np.float32)
        if variable not in self._accumulator:
            self._accumulator[variable] = []
        self._accumulator[variable].append((ts_str, arr))

    def flush(self):
        """Write all accumulated data to the database in one transaction."""
        if not self._accumulator:
            return

        rows = []
        for variable, records in self._accumulator.items():
            for ts_str, arr in records:
                rows.append((variable, ts_str, arr.tobytes()))

        self._conn.executemany(
            "INSERT OR REPLACE INTO data (variable, timestamp, values_blob) VALUES (?, ?, ?)",
            rows,
        )
        self._conn.commit()
        n_vars = len(self._accumulator)
        self._accumulator.clear()
        logger.info("Flushed %d variables to %s", n_vars, self.db_path.name)

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Binary web file generation
    # ------------------------------------------------------------------

    def generate_web_files(self, web_dir=None):
        """Generate compact binary files for fast website loading.

        Each .bin file: uint32 n_points, uint32 n_hours,
        then n_hours × n_points float32 values (contiguous).
        Browser loads via fetch() → ArrayBuffer → Float32Array.

        ~80MB for 28K points × 744 hours vs ~300MB CSV.
        No parsing overhead — instant ArrayBuffer access.
        """
        if web_dir is None:
            web_dir = self.month_dir / "web"
        web_dir = Path(web_dir)
        web_dir.mkdir(parents=True, exist_ok=True)

        # Get all variables
        cursor = self._conn.execute(
            "SELECT DISTINCT variable FROM data ORDER BY variable"
        )
        variables = [row[0] for row in cursor]

        for variable in variables:
            cursor = self._conn.execute(
                "SELECT timestamp, values_blob FROM data WHERE variable = ? ORDER BY timestamp",
                (variable,),
            )
            rows = cursor.fetchall()
            if not rows:
                continue

            n_hours = len(rows)
            first_arr = np.frombuffer(rows[0][1], dtype=np.float32)
            n_points = len(first_arr)

            # Write binary file
            bin_path = web_dir / f"{variable}.bin"
            with open(bin_path, "wb") as f:
                # Header
                f.write(struct.pack("<II", n_points, n_hours))
                # Timestamps as newline-separated string block
                timestamps = [r[0] for r in rows]
                ts_block = "\n".join(timestamps).encode("utf-8")
                f.write(struct.pack("<I", len(ts_block)))
                f.write(ts_block)
                # Data: n_hours × n_points float32
                for _, blob in rows:
                    f.write(blob)

            size_mb = bin_path.stat().st_size / 1048576
            logger.info("Generated %s (%.1f MB, %d hours × %d points)",
                        bin_path.name, size_mb, n_hours, n_points)

        # Write variable list
        manifest = web_dir / "variables.json"
        import json
        with open(manifest, "w") as f:
            json.dump(variables, f)

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def export_csv(self, variable, output_path=None, precision=None):
        """Export a single variable to CSV format.

        Args:
            variable: Variable name (e.g., "cfwi/FWI" or "FWI").
            output_path: Output file path. Default: month_dir/csv/{variable}.csv.
            precision: Float decimal places. None = full precision.

        Returns:
            Path to the exported CSV file.
        """
        import pandas as pd

        cursor = self._conn.execute(
            "SELECT timestamp, values_blob FROM data WHERE variable = ? ORDER BY timestamp",
            (variable,),
        )
        rows = cursor.fetchall()
        if not rows:
            raise ValueError(f"No data found for variable '{variable}'")

        # Get point IDs
        pts = self._conn.execute(
            "SELECT point_id FROM points ORDER BY point_id"
        ).fetchall()
        point_ids = [r[0] for r in pts]

        timestamps = [r[0] for r in rows]
        n_points = len(np.frombuffer(rows[0][1], dtype=np.float32))

        # Build matrix
        data_matrix = np.column_stack([
            np.frombuffer(blob, dtype=np.float32)
            for _, blob in rows
        ])

        df = pd.DataFrame(data_matrix, index=point_ids[:n_points], columns=timestamps)
        df.index.name = "point_id"

        if output_path is None:
            csv_dir = self.month_dir / "csv"
            csv_dir.mkdir(parents=True, exist_ok=True)
            output_path = csv_dir / f"{self.year}_{self.month:02d}_{variable}.csv"

        float_fmt = f"%.{precision}f" if precision else None
        df.to_csv(output_path, float_format=float_fmt)
        logger.info("Exported %s to %s", variable, output_path)
        return Path(output_path)

    def export_all_csv(self, output_dir=None, precision=None):
        """Export all variables to CSV files."""
        cursor = self._conn.execute(
            "SELECT DISTINCT variable FROM data ORDER BY variable"
        )
        variables = [row[0] for row in cursor]

        paths = []
        for var in variables:
            path = self.export_csv(var, precision=precision)
            paths.append(path)

        return paths

    # ------------------------------------------------------------------
    # Read API (for future real-time use)
    # ------------------------------------------------------------------

    def get_hour(self, variable, timestamp_str):
        """Get data for a single variable at a single timestamp.

        Returns:
            numpy float32 array of shape (n_points,), or None.
        """
        cursor = self._conn.execute(
            "SELECT values_blob FROM data WHERE variable = ? AND timestamp = ?",
            (variable, timestamp_str),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return np.frombuffer(row[0], dtype=np.float32).copy()

    def get_timestamps(self, variable):
        """Get all available timestamps for a variable."""
        cursor = self._conn.execute(
            "SELECT DISTINCT timestamp FROM data WHERE variable = ? ORDER BY timestamp",
            (variable,),
        )
        return [row[0] for row in cursor]

    def get_variables(self):
        """Get all available variable names."""
        cursor = self._conn.execute(
            "SELECT DISTINCT variable FROM data ORDER BY variable"
        )
        return [row[0] for row in cursor]
