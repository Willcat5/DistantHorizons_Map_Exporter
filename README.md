# DhMap Exporter

Export Minecraft [Distant Horizons](https://modrinth.com/mod/distanthorizons) mod LOD data to PNG map images.

Renders a top-down view of your world using the compressed LOD data stored in the mod's SQLite database.

## Requirements

- Python 3.10+
- A Distant Horizons SQLite database file (`DistantHorizons.sqlite`)

## Install

```bash
pip install -r requirements.txt
```

Dependencies: `zstandard`, `Pillow`, `numpy`

## Usage

### Basic render

```bash
python main.py render path/to/DistantHorizons.sqlite
```

This opens an interactive prompt showing your world bounds and lets you optionally crop to a region. Output goes to `renders/<db_name>.png` by default.

### All options

```bash
python main.py render <database> [options]
```

| Flag | Description                                                                                                                                                                                                     |
|------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `-o`, `--output` | Output PNG path (default: `renders/<db_name>.png`)                                                                                                                                                              |
| `-s`, `--scale` | Downscale factor for performance/previews. `2` = half res, `4` = quarter res, etc.                                                                                                                              |
| `-y`, `--y-level` | Render a specific Y level instead of top-down                                                                                                                                                                   |
| `--all-below` | With `-y`, show all blocks at or below that Y level                                                                                                                                                             |
| `--find` | Highlight all occurrences of a block (e.g. `white_wool`) Uses fnmatch for wildcarding.<br/> Can accept multiple blocks at once (eg. `barrel chest white_wool *_bed`)<br/>Each term will be given its own color. |
| `--find-size` | Highlight square radius for `--find`. Default `1` = 3x3, use `3` for 7x7                                                                                                                                        |
| `--crop` | Crop to only render certain pixel region on the full-res map: `X1 Y1 X2 Y2` (recommended for cutting up large detailed renders)                                                                                 |

### Examples

**Quarter resolution:**
```bash
python main.py render world.sqlite -s 4
```

**Find beds:**
```bash
python main.py render world.sqlite --find white_bed --find-size 3
```

**Crop to a region** (pixel coords on the full map):
```bash
python main.py render world.sqlite --crop 1000 2000 3000 4000
```

### Memory

Large worlds can produce very large images. The tool warns you before allocating if the image would exceed ~4 GB RAM, and suggests a `-s` value to use instead.

| World size | Full res | `-s 4` | `-s 8` |
|-----------|----------|--------|--------|
| 60k x 60k blocks | ~10 GB | ~650 MB | ~160 MB |
| 95k x 90k blocks | ~24 GB | ~1.5 GB | ~380 MB |

### Where is the database?

The Distant Horizons database is typically at:
- **Default launcher:** `C:\Users\<User>\AppData\Roaming\.minecraft\Distant_Horizons_server_data\<server>\<World>\DistantHorizons.sqlite`
- **Modrinth:**         `C:\Users\<User>\AppData\Roaming\ModrinthApp\profiles\<profile>\Distant_Horizons_server_data\<server>\<World>\DistantHorizons.sqlite`

## How it works

1. Reads the DH `FullData` table at detail level 0 (1-block resolution)
2. Decompresses zstd-compressed data blobs
3. Parses the V2 binary format (5-pass deserialization)
4. Maps block state names to RGB colors from `block_colors.py`
5. Writes pixels to a numpy array, then saves as PNG

Each data point in the DH format encodes a block ID, vertical height range, Y offset, and light levels as a packed 64-bit integer. The tool extracts block IDs and maps them to colors using a state-independent lookup table (722 base block names).
