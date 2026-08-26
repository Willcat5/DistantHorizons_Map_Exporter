"""
Reads the Distant Horizons SQLite database, decompresses blobs,
and hands off to the decoder for binary format parsing.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from dh_decoder import (
    SectionData,
    DataColumn,
    deserialize_mapping,
    deserialize_data_blob_v2,
    deserialize_data_blob_v1,
    deserialize_generation_steps,
    deserialize_compression_modes,
    decompress_blob,
    MappingEntry,
    WIDTH,
)


@dataclass
class DatabaseInfo:
    """Summary info about a DH database file."""
    path: Path
    total_rows: int
    detail_levels: list[int]
    compression_modes: list[int]
    pos_range: tuple[int, int, int, int]  # (min_x, max_x, min_z, max_z)


def get_database_info(db_path: str | Path) -> DatabaseInfo:
    """Scan the database to get summary information without loading all data."""
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM FullData")
    total_rows = cursor.fetchone()[0]

    cursor.execute("SELECT DISTINCT DetailLevel FROM FullData")
    detail_levels = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT DISTINCT CompressionMode FROM FullData")
    compression_modes = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT MIN(PosX), MAX(PosX), MIN(PosZ), MAX(PosZ) FROM FullData")
    pos_range = cursor.fetchone()

    conn.close()
    return DatabaseInfo(
        path=db_path,
        total_rows=total_rows,
        detail_levels=detail_levels,
        compression_modes=compression_modes,
        pos_range=pos_range,
    )


def iter_rows(db_path: str | Path, detail_level: int = 0,
              min_pos_x: int | None = None, max_pos_x: int | None = None,
              min_pos_z: int | None = None, max_pos_z: int | None = None):
    """
    Yield raw rows from the FullData table for a given detail level.
    Each row is a dict with keys matching the column names.
    Optional position filters restrict which sections are returned.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = (
        "SELECT DetailLevel, PosX, PosZ, Data, Mapping, "
        "       ColumnGenerationStep, ColumnWorldCompressionMode, "
        "       DataFormatVersion, CompressionMode, ApplyToParent "
        "FROM FullData WHERE DetailLevel = ?"
    )
    params = [detail_level]
    if min_pos_x is not None:
        query += " AND PosX >= ?"
        params.append(min_pos_x)
    if max_pos_x is not None:
        query += " AND PosX <= ?"
        params.append(max_pos_x)
    if min_pos_z is not None:
        query += " AND PosZ >= ?"
        params.append(min_pos_z)
    if max_pos_z is not None:
        query += " AND PosZ <= ?"
        params.append(max_pos_z)

    cursor.execute(query, params)

    for row in cursor:
        yield dict(row)

    conn.close()


def decode_row(row: dict) -> SectionData | None:
    """
    Decode a single database row into a SectionData object.
    Returns None if the row has no data or is invalid.
    """
    data_blob = row.get("Data")
    mapping_blob = row.get("Mapping")
    gen_step_blob = row.get("ColumnGenerationStep")
    comp_mode_blob = row.get("ColumnWorldCompressionMode")
    data_format = row.get("DataFormatVersion", 2)
    compression_mode = row.get("CompressionMode", 0)

    if data_blob is None or mapping_blob is None:
        return None

    if len(data_blob) == 0 or len(mapping_blob) == 0:
        return None

    # Decompress the blobs
    try:
        data_decompressed = decompress_blob(data_blob, compression_mode)
    except Exception as e:
        print(f"  Warning: Failed to decompress Data blob: {e}")
        return None

    try:
        mapping_decompressed = decompress_blob(mapping_blob, compression_mode)
    except Exception as e:
        print(f"  Warning: Failed to decompress Mapping blob: {e}")
        return None

    # Parse the mapping
    try:
        mapping_entries = deserialize_mapping(mapping_decompressed)
    except Exception as e:
        print(f"  Warning: Failed to parse Mapping blob: {e}")
        return None

    # Parse the data columns
    columns: dict[tuple[int, int], DataColumn] = {}
    try:
        if data_format == 1:
            deserialize_data_blob_v1(data_decompressed, columns)
        else:
            deserialize_data_blob_v2(data_decompressed, columns)
    except Exception as e:
        print(f"  Warning: Failed to parse Data blob: {e}")
        return None

    # Parse generation steps and compression modes
    gen_steps = []
    comp_modes = []
    if gen_step_blob and len(gen_step_blob) > 0:
        try:
            gen_steps = deserialize_generation_steps(
                decompress_blob(gen_step_blob, compression_mode)
            )
        except Exception:
            pass
    if comp_mode_blob and len(comp_mode_blob) > 0:
        try:
            comp_modes = deserialize_compression_modes(
                decompress_blob(comp_mode_blob, compression_mode)
            )
        except Exception:
            pass

    section = SectionData(
        detail_level=row["DetailLevel"],
        pos_x=row["PosX"],
        pos_z=row["PosZ"],
        columns=columns,
        generation_steps=gen_steps,
        compression_modes=comp_modes,
        data_format_version=data_format,
        apply_to_parent=bool(row.get("ApplyToParent", False)),
    )

    # Attach the mapping entries to the section for later use
    section.mapping_entries = mapping_entries  # type: ignore

    return section


def iter_sections(db_path: str | Path, detail_level: int = 0,
                   min_pos_x: int | None = None, max_pos_x: int | None = None,
                   min_pos_z: int | None = None, max_pos_z: int | None = None):
    """
    Yield decoded SectionData objects from the database.
    Attaches .mapping_entries (list[MappingEntry]) to each section.
    Optional position filters restrict which sections are returned.
    """
    for row in iter_rows(db_path, detail_level, min_pos_x, max_pos_x, min_pos_z, max_pos_z):
        section = decode_row(row)
        if section is not None:
            yield section
