"""
CLI entry point for dhmap.

Usage:
  python -m main render <sqlite_file> [options]   Render the map to a PNG
"""

import argparse
import fnmatch
import sys
import time
from pathlib import Path

from dh_reader import iter_sections
from dh_decoder import WIDTH


def _progress(count: int, total: int, label: str, width: int = 40):
    """Print an inline progress bar."""
    pct = count / total if total else 1
    filled = int(width * pct)
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r  [{bar}] {count}/{total} ({pct:.0%}) {label}", end="", flush=True)


# def cmd_scan_dict(args):
#     """Scan the database for all unique block states and generate a color_map.py template."""
#     db_path = Path(args.sqlite_file)
#     if not db_path.exists():
#         print(f"Error: File not found: {db_path}")
#         sys.exit(1)
#
#     print(f"Scanning {db_path} ...")
#     info = get_database_info(db_path)
#     print(f"  Rows: {info.total_rows}")
#     print(f"  Detail levels: {info.detail_levels}")
#     print(f"  Compression modes: {info.compression_modes}")
#     print(f"  Pos range: X=[{info.pos_range[0]}, {info.pos_range[1]}], Z=[{info.pos_range[2]}, {info.pos_range[3]}]")
#
#     # Collect all unique block state strings
#     all_block_states: dict[str, str] = {}  # block_state -> biome (for context)
#     section_count = 0
#     start = time.time()
#
#     for section in iter_sections(db_path, detail_level=0):
#         section_count += 1
#         if section_count % 100 == 0:
#             elapsed = time.time() - start
#             print(f"  Processed {section_count} sections ({elapsed:.1f}s) ...", end="\r")
#
#         mapping = getattr(section, "mapping_entries", [])
#         for entry in mapping:
#             bs = entry.block_state
#             if bs not in all_block_states:
#                 all_block_states[bs] = entry.biome
#
#     elapsed = time.time() - start
#     print(f"\n  Done. Processed {section_count} sections in {elapsed:.1f}s")
#     print(f"  Found {len(all_block_states)} unique block states")
#
#     # Generate color_map.py
#     output_path = Path("color_map.py")
#     lines = [
#         "# Auto-generated block state color map",
#         f"# Source: {db_path.name} ({len(all_block_states)} unique block states)",
#         "#",
#         "# Fill in RGB tuples for each block state.",
#         "# Use (R, G, B) format with values 0-255.",
#         "#",
#         "# TIP: You can use an LLM or Minecraft wiki to help fill in colors.",
#         "# Example entries:",
#         '#   "minecraft:grass_block[snowy=false]": (86, 148, 48),',
#         '#   "minecraft:stone": (125, 125, 125),',
#         "",
#         "BLOCK_COLORS = {",
#     ]
#
#     for bs in sorted(all_block_states.keys()):
#         biome = all_block_states[bs]
#         lines.append(f'    # Biome: {biome}')
#         lines.append(f'    "{bs}": (128, 128, 128),')
#
#     lines.append("}")
#     lines.append("")
#     lines.append("# Default color for unmapped block states")
#     lines.append("DEFAULT_COLOR = (128, 128, 128)")
#     lines.append("")
#
#     output_path.write_text("\n".join(lines), encoding="utf-8")
#     print(f"  Wrote {output_path} with {len(all_block_states)} entries")
#     print(f"\n  Edit color_map.py to fill in the correct RGB colors, then run:")
#     print(f"    python main.py render {db_path} -o map.png")
#
#
# def cmd_scan_textures(args):
#     """Scan the database and match block states to texture PNGs to generate color_map.py."""
#     from scan_textures import run
#
#     db_path = Path(args.sqlite_file)
#     if not db_path.exists():
#         print(f"Error: File not found: {db_path}")
#         sys.exit(1)
#
#     texture_dir = Path(args.texture_dir)
#     if not texture_dir.exists():
#         print(f"Error: Texture directory not found: {texture_dir}")
#         sys.exit(1)
#
#     run(db_path, texture_dir)


def _is_air(mapping: list, dp_id: int) -> bool:
    """Check if a data point ID corresponds to an air block."""
    if dp_id < len(mapping):
        bs = mapping[dp_id].block_state
        return bs == "AIR" or bs.endswith(":air") or bs.endswith(":cave_air") or bs.endswith(":void_air")
    return False


def _get_level_min_y(mapping: list) -> int:
    """Detect the dimension from biome strings and return level_min_y.

    DH encodes bottomY as relative to the dimension's minimum Y:
      - Nether:    level_min_y = 0   (Nether goes from 0 to 256)
      - Overworld: level_min_y = -64 (Overworld goes from -64 to 320)
      - End:       level_min_y = 0   (End goes from 0 to 256)

    We detect the dimension by checking biome names in the mapping.
    """
    NETHER_BIOMES = {"nether_wastes", "soul_sand_valley", "crimson_forest",
                     "warped_forest", "basalt_deltas"}
    for entry in mapping:
        biome_suffix = entry.biome.split(":")[-1].lower()
        if biome_suffix in NETHER_BIOMES or "nether" in biome_suffix:
            return 0
    # Default to Overworld
    return -64


def cmd_render(args):
    """Render the map from the database to a PNG."""
    import numpy as np
    from PIL import Image
    import sqlite3 as _sqlite3

    total_start = time.time()
    db_path = Path(args.sqlite_file)
    if not db_path.exists():
        print(f"Error: File not found: {db_path}")
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_dir = Path("renders")
        output_dir.mkdir(exist_ok=True)
        output_path = output_dir / (db_path.stem + ".png")
    interrupted = False

    # Load block color map
    try:
        from block_colors import BLOCK_COLORS, DEFAULT_COLOR
        print(f"Loaded block_colors.py with {len(BLOCK_COLORS)} base block colors")
    except ImportError:
        BLOCK_COLORS = {}
        DEFAULT_COLOR = (128, 128, 128)

    y_level = args.y_level
    show_all_below = args.all_below
    find_blocks = args.find  # list of block names, or None
    find_size = args.find_size

    if find_blocks is not None:
        print(f"Block finder: searching for {len(find_blocks)} block(s): {', '.join(find_blocks)}")
    elif y_level is not None:
        if show_all_below:
            print(f"Rendering: show all blocks at/below Y={y_level}")
        else:
            print(f"Rendering: blocks at Y={y_level}")
    else:
        print("Rendering: top-down view (topmost non-air block per column)")

    # --- Step 1: Get section bounds without loading any blob data ---
    print("Querying section bounds ...")
    t = time.time()
    with _sqlite3.connect(str(db_path)) as conn:
        bounds_query = (
            "SELECT MIN(PosX), MAX(PosX), MIN(PosZ), MAX(PosZ), COUNT(*) "
            "FROM FullData WHERE DetailLevel = 0"
        )
        bounds = conn.execute(bounds_query).fetchone()
    min_sec_x, max_sec_x, min_sec_z, max_sec_z, total_rows = bounds
    print(f"  {total_rows} rows, bounds in {time.time()-t:.1f}s")

    # Compute image dimensions
    world_min_x = min_sec_x * WIDTH
    world_max_x = max_sec_x * WIDTH + WIDTH - 1
    world_min_z = min_sec_z * WIDTH
    world_max_z = max_sec_z * WIDTH + WIDTH - 1

    scale = args.scale
    full_width = world_max_x - world_min_x + 1
    full_height = world_max_z - world_min_z + 1

    # Handle --crop: pixel coordinates on full-res map
    crop = args.crop
    if crop is None:
        img_w_full = (full_width + scale - 1) // scale
        img_h_full = (full_height + scale - 1) // scale
        print(f"\n  Full map: {full_width} x {full_height} blocks")
        if scale > 1:
            print(f"  Output at -s {scale}: {img_w_full} x {img_h_full} pixels")
        else:
            print(f"  Output: {img_w_full} x {img_h_full} pixels")
        print(f"\n  Crop to a region? Enter pixel coords as: X1 Y1 X2 Y2")
        print(f"  (or press Enter to render the full map)")
        try:
            user_input = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            user_input = ""
        if user_input:
            parts = user_input.split()
            if len(parts) == 4:
                try:
                    crop = tuple(int(p) for p in parts)
                except ValueError:
                    print("  Invalid input, rendering full map.")
            else:
                print("  Expected 4 numbers, rendering full map.")
    crop_min_x = crop_min_z = None
    crop_max_x = crop_max_z = None
    if crop is not None:
        px1, py1, px2, py2 = crop
        if px1 > px2:
            px1, px2 = px2, px1
        if py1 > py2:
            py1, py2 = py2, py1
        # Clamp to full image bounds
        px1, py1 = max(0, px1), max(0, py1)
        px2, py2 = min(full_width, px2), min(full_height, py2)
        if px1 >= px2 or py1 >= py2:
            print(f"Error: crop region is empty ({px1},{py1}) to ({px2},{py2})")
            sys.exit(1)
        crop_min_x = world_min_x + px1
        crop_min_z = world_min_z + py1
        crop_max_x = world_min_x + px2
        crop_max_z = world_min_z + py2
        img_width = (px2 - px1 + scale - 1) // scale
        img_height = (py2 - py1 + scale - 1) // scale
        print(f"  Cropping to pixels ({px1},{py1})-({px2},{py2}) on full map")
        print(f"  World range: X=[{crop_min_x}, {crop_max_x-1}], Z=[{crop_min_z}, {crop_max_z-1}]")
    else:
        img_width = (full_width + scale - 1) // scale
        img_height = (full_height + scale - 1) // scale
    print(f"  World block range: X=[{world_min_x}, {world_max_x}], Z=[{world_min_z}, {world_max_z}]")
    if scale > 1:
        print(f"  Full size: {full_width} x {full_height}, scaled {scale}x: {img_width} x {img_height} pixels")
    else:
        print(f"  Image size: {img_width} x {img_height} pixels")

    pixel_count = img_width * img_height
    mem_bytes = pixel_count * 3  # uint8 RGB
    mem_gb = mem_bytes / (1024**3)
    if mem_gb > 4:
        min_scale = int(mem_gb / 4) + 1
        print(f"  WARNING: Image needs ~{mem_gb:.1f} GB RAM ({pixel_count/1e6:.0f}M pixels).")
        print(f"  Re-run with -s {min_scale} to reduce to ~{mem_gb/min_scale**2:.1f} GB.")
        print(f"  Proceeding anyway ...")
    elif pixel_count > 200_000_000:
        print(f"  WARNING: Image is very large ({pixel_count / 1e6:.0f}M pixels, ~{mem_gb:.1f} GB).")

    # Compute section position bounds for the query filter
    sec_filter_x1 = min_sec_x
    sec_filter_x2 = max_sec_x
    sec_filter_z1 = min_sec_z
    sec_filter_z2 = max_sec_z
    if crop is not None:
        sec_filter_x1 = crop_min_x // WIDTH
        sec_filter_x2 = (crop_max_x - 1) // WIDTH
        sec_filter_z1 = crop_min_z // WIDTH
        sec_filter_z2 = (crop_max_z - 1) // WIDTH

    # Count sections in crop region
    if crop is not None:
        with _sqlite3.connect(str(db_path)) as conn:
            total_rows = conn.execute(
                "SELECT COUNT(*) FROM FullData WHERE DetailLevel = 0 "
                "AND PosX >= ? AND PosX <= ? AND PosZ >= ? AND PosZ <= ?",
                (sec_filter_x1, sec_filter_x2, sec_filter_z1, sec_filter_z2),
            ).fetchone()[0]

    # --- Step 2: Single streaming pass — render as we go ---
    print("Rendering ...")
    pixel_count_num = 0
    unknown_blocks = set()
    find_positions = []  # list of (sx, sz) scaled pixel coords for --find
    start = time.time()

    # Use numpy array for fast pixel writes
    img_array = np.zeros((img_height, img_width, 3), dtype=np.uint8)

    # Bit masks (inlined for hot path)
    _ID_MASK = 0xFFFFFFFF
    _HEIGHT_SHIFT = 32
    _MIN_Y_SHIFT = 44
    _HEIGHT_MASK = 0xFFF
    _MIN_Y_MASK = 0xFFF

    # Precompute default color array
    default_rgb = np.array(DEFAULT_COLOR, dtype=np.uint8)

    try:
      row_idx = 0
      for section in iter_sections(db_path, detail_level=0,
                                  min_pos_x=sec_filter_x1, max_pos_x=sec_filter_x2,
                                  min_pos_z=sec_filter_z1, max_pos_z=sec_filter_z2):
        row_idx += 1
        if row_idx % 50 == 0:
            _progress(row_idx, total_rows, "sections")

        mapping = getattr(section, "mapping_entries", [])
        if not mapping:
            continue

        # Check if section has any non-empty inner columns
        has_data = False
        for rel_x in range(1, WIDTH - 1):
            if has_data:
                break
            for rel_z in range(1, WIDTH - 1):
                col = section.columns.get((rel_x, rel_z))
                if col and len(col) > 0:
                    has_data = True
                    break
        if not has_data:
            continue

        # Precompute per-section lookups (discarded after this section)
        air_ids = set()
        id_to_color = {}
        find_ids = set()  # mapping IDs that match any --find target
        id_to_find_name = {}  # mapping ID -> find block name
        for i, entry in enumerate(mapping):
            bs = entry.block_state
            if bs == "AIR" or bs.endswith(":air") or bs.endswith(":cave_air") or bs.endswith(":void_air"):
                air_ids.add(i)
            # Strip namespace, _STATE_ suffix, and properties to get base block name
            name = bs.split(":", 1)[1] if ":" in bs else bs
            idx = name.find("_STATE_")
            base = name[:idx] if idx != -1 else name
            brace = base.find("{")
            if brace != -1:
                base = base[:brace]
            id_to_color[i] = BLOCK_COLORS.get(base, DEFAULT_COLOR)
            if find_blocks is not None and any(fnmatch.fnmatch(base, pat) for pat in find_blocks):
                find_ids.add(i)
                id_to_find_name[i] = base

        level_min_y = _get_level_min_y(mapping)

        sec_world_x = section.pos_x * WIDTH
        sec_world_z = section.pos_z * WIDTH

        for rel_x in range(1, WIDTH - 1):
            wx = sec_world_x + rel_x
            base_x = wx - world_min_x
            for rel_z in range(1, WIDTH - 1):
                col = section.columns.get((rel_x, rel_z))
                if col is None or len(col) == 0:
                    continue

                wz = sec_world_z + rel_z

                target_dp = 0
                for dp in col.data_points:
                    if dp == 0:
                        continue
                    dp_id = dp & _ID_MASK
                    if dp_id in air_ids:
                        continue

                    # Block finder: check ALL data points in column
                    if find_ids and dp_id in find_ids:
                        if crop is not None:
                            pz = wz - crop_min_z
                        else:
                            pz = wz - world_min_z
                        find_positions.append((base_x // scale, pz // scale, id_to_find_name.get(dp_id, "")))

                    # DP selection for rendering
                    if y_level is not None:
                        dp_bottom_y = (dp >> _MIN_Y_SHIFT) & _MIN_Y_MASK
                        dp_height = (dp >> _HEIGHT_SHIFT) & _HEIGHT_MASK
                        abs_y = level_min_y + dp_bottom_y
                        abs_top = abs_y + dp_height
                        if show_all_below:
                            if abs_top <= y_level + 1:
                                target_dp = dp
                                break
                        else:
                            if abs_y <= y_level < abs_top:
                                target_dp = dp
                                break
                    else:
                        # Top-down: first non-air DP
                        if target_dp == 0:
                            target_dp = dp
                        if not find_ids:
                            break

                if target_dp == 0:
                    continue

                dp_id = target_dp & _ID_MASK
                color = id_to_color.get(dp_id, DEFAULT_COLOR)

                if dp_id not in id_to_color and dp_id not in unknown_blocks:
                    unknown_blocks.add(dp_id)

                if crop is not None:
                    sx = (wx - crop_min_x) // scale
                    sz = (wz - crop_min_z) // scale
                else:
                    sx = base_x // scale
                    sz = (wz - world_min_z) // scale
                if 0 <= sx < img_width and 0 <= sz < img_height:
                    img_array[sz, sx] = color
                    pixel_count_num += 1

      _progress(row_idx, total_rows, "sections")
      print()
    except KeyboardInterrupt:
      interrupted = True
      _progress(row_idx, total_rows, "sections")
      print()
      print("  Interrupted")

    elapsed = time.time() - start
    if pixel_count_num > 0:
        print(f"  Done. {pixel_count_num} pixels in {elapsed:.1f}s")
    else:
        print(f"  No pixels rendered in {elapsed:.1f}s")

    # Block finder: draw highlights with cyan center
    if find_blocks is not None:
        _FIND_COLORS = [
            (255, 0, 255),    # pink
            (255, 165, 0),    # orange
            (0, 255, 0),      # green
            (0, 165, 255),    # blue
            (255, 255, 0),    # yellow
            (0, 255, 255),    # cyan
            (255, 0, 165),    # magenta
        ]
        _FIND_COLOR_NAMES = {
            (255, 0, 255): "Pink",
            (255, 165, 0): "Orange",
            (0, 255, 0): "Green",
            (0, 165, 255): "Blue",
            (255, 255, 0): "Yellow",
            (0, 255, 255): "Cyan",
            (255, 0, 165): "Magenta",
        }
        # One color per --find pattern
        pattern_colors = {}
        for i, pat in enumerate(find_blocks):
            pattern_colors[pat] = _FIND_COLORS[i % len(_FIND_COLORS)]
        # Count per block
        counts = {}
        for _, _, name in find_positions:
            counts[name] = counts.get(name, 0) + 1
        for name, count in counts.items():
            matched_pat = name
            for pat in find_blocks:
                if fnmatch.fnmatch(name, pat):
                    matched_pat = pat
                    break
            rgb = pattern_colors.get(matched_pat, (255, 0, 255))
            color_name = _FIND_COLOR_NAMES.get(rgb, "Custom")
            print(f"  Found {count} of '{name}' (highlighted {color_name})")
        # Draw highlights — surrounds first, then centers on top
        for fx, fz, name in find_positions:
            # Find which pattern matched this block name
            matched_pat = name
            for pat in find_blocks:
                if fnmatch.fnmatch(name, pat):
                    matched_pat = pat
                    break
            rgb = pattern_colors.get(matched_pat, (255, 0, 255))
            surround = np.array(rgb, dtype=np.uint8)
            for dx in range(-find_size, find_size + 1):
                for dz in range(-find_size, find_size + 1):
                    if dx == 0 and dz == 0:
                        continue  # skip center for now
                    px, pz2 = fx + dx, fz + dz
                    if 0 <= px < img_width and 0 <= pz2 < img_height:
                        img_array[pz2, px] = surround
        for fx, fz, name in find_positions:
            matched_pat = name
            for pat in find_blocks:
                if fnmatch.fnmatch(name, pat):
                    matched_pat = pat
                    break
            rgb = pattern_colors.get(matched_pat, (255, 255, 255))
            # Light centers for all colors except yellow (which darkens)
            if rgb == (255, 255, 0):
                center = np.array([rgb[0] // 2, rgb[1] // 2, rgb[2] // 2], dtype=np.uint8)
            else:
                center = np.array([(rgb[0] + 255) // 2, (rgb[1] + 255) // 2, (rgb[2] + 255) // 2], dtype=np.uint8)
            px, pz2 = fx, fz
            if 0 <= px < img_width and 0 <= pz2 < img_height:
                img_array[pz2, px] = center
        print(f"  Highlighted {len(find_positions)} locations (light centers, colored surround)")

    if unknown_blocks:
        print(f"\n  {len(unknown_blocks)} unmapped block states (using default color)")
        unmapped_names = set()
        for uid in unknown_blocks:
            # Can't easily resolve names in single pass, just count
            pass
        print(f"    (Names not tracked in single-pass mode)")

    # Convert numpy array to PIL Image and save
    img = Image.fromarray(img_array)
    img.save(str(output_path))
    file_size = output_path.stat().st_size
    if file_size > 1_000_000:
        size_str = f"{file_size / 1_000_000:.1f} MB"
    else:
        size_str = f"{file_size / 1_000:.0f} KB"
    status = " (interrupted — partial render)" if interrupted else ""
    print(f"\n  Saved to {output_path} ({size_str}){status}")

    total_elapsed = time.time() - total_start
    print(f"  Total runtime: {total_elapsed:.1f}s")


def main():
    parser = argparse.ArgumentParser(
        description="Export Distant Horizons SQLite LOD data to PNG maps."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan-dict subcommand (dev tool — commented out for release)
    # scan_parser = subparsers.add_parser(
    #     "scan-dict",
    #     help="Scan the database and generate a color_map.py template",
    # )
    # scan_parser.add_argument("sqlite_file", help="Path to DistantHorizons.sqlite")
    # scan_parser.set_defaults(func=cmd_scan_dict)

    # scan-textures subcommand (dev tool — commented out for release)
    # tex_parser = subparsers.add_parser(
    #     "scan-textures",
    #     help="Match block states to texture PNGs and generate color_map.py",
    # )
    # tex_parser.add_argument("sqlite_file", help="Path to DistantHorizons.sqlite")
    # tex_parser.add_argument(
    #     "--texture-dir",
    #     default=r"C:\Users\Willf\Modding\dhmapblock",
    #     help="Path to folder of block texture PNGs (default: dhmapblock)",
    # )
    # tex_parser.set_defaults(func=cmd_scan_textures)

    # render subcommand
    render_parser = subparsers.add_parser(
        "render",
        help="Render the map to a PNG file",
    )
    render_parser.add_argument("sqlite_file", help="Path to DistantHorizons.sqlite")
    render_parser.add_argument("-o", "--output", default=None, help="Output PNG path (default: renders/<db_name>.png)")
    render_parser.add_argument("-y", "--y-level", type=int, default=None, help="Y level to render (omit for top-down view)")
    render_parser.add_argument("--all-below", action="store_true", help="Show all blocks at or below the specified Y level")
    render_parser.add_argument("-s", "--scale", type=int, default=1, help="Downscale factor (2=half res, 4=quarter res)")
    render_parser.add_argument("--find", type=str, nargs="+", default=None, metavar="BLOCK", help="Highlight blocks by name or wildcard (e.g. --find *bed diamond_ore *dirt)")
    render_parser.add_argument("--find-size", type=int, default=1, metavar="N", help="Highlight radius for --find (default: 1 = 3x3, use 3 for 7x7)")
    render_parser.add_argument("--crop", type=int, nargs=4, default=None, metavar=("X1", "Y1", "X2", "Y2"), help="Crop to pixel region on the full-res map (e.g. --crop 1000 2000 3000 4000)")
    render_parser.set_defaults(func=cmd_render)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
