"""
Decodes the binary data blobs from Distant Horizons' SQLite database.

Ported from the Java source code in distant-horizons-core:
  - FullDataSourceV2DTO.java  (blob read/write)
  - FullDataPointUtil.java    (data point bit layout)
  - VarintUtil.java           (varint + zigzag encoding)
  - FullDataPointIdMap.java   (mapping blob deserialization)
"""

import struct
from io import BytesIO
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants (from FullDataPointUtil.java)
# ---------------------------------------------------------------------------
# Data point bit layout (64-bit long):
#   SL SL SL SL  BL BL BL BL  (top bits, 60-63 and 56-59)
#   MY MY MY MY  MY MY MY MY  (44-55)
#   MY MY MY MY  HI HI HI HI  (32-43)
#   HI HI HI HI  HI HI HI HI
#   ID ID ID ID  ID ID ID ID
#   ID ID ID ID  ID ID ID ID
#   ID ID ID ID  ID ID ID ID
#   ID ID ID ID  ID ID ID ID  (bottom bits, 0-31)

ID_WIDTH = 32
HEIGHT_WIDTH = 12
MIN_Y_WIDTH = 12
SKY_LIGHT_WIDTH = 4
BLOCK_LIGHT_WIDTH = 4

ID_OFFSET = 0
HEIGHT_OFFSET = ID_OFFSET + ID_WIDTH       # 32
MIN_Y_OFFSET = HEIGHT_OFFSET + HEIGHT_WIDTH  # 44
SKY_LIGHT_OFFSET = MIN_Y_OFFSET + MIN_Y_WIDTH  # 56
BLOCK_LIGHT_OFFSET = SKY_LIGHT_OFFSET + SKY_LIGHT_WIDTH  # 60

ID_MASK = 0xFFFFFFFF          # 32 bits
HEIGHT_MASK = 0xFFF           # 12 bits
MIN_Y_MASK = 0xFFF            # 12 bits
SKY_LIGHT_MASK = 0xF          # 4 bits
BLOCK_LIGHT_MASK = 0xF        # 4 bits

EMPTY_DATA_POINT = 0

# Section width at detail level 0
WIDTH = 64

# World Y constants (Minecraft 1.18+)
WORLD_MIN_Y = -64
WORLD_MAX_Y = 320

# Data format versions
DATA_FORMAT_V1 = 1
DATA_FORMAT_V2 = 2

# Compression mode values
COMPRESS_UNCOMPRESSED = 0
COMPRESS_LZ4 = 1
COMPRESS_ZSTD_STREAM = 2  # deprecated
COMPRESS_LZMA2 = 3
COMPRESS_ZSTD_BLOCK = 4


# ---------------------------------------------------------------------------
# Data point helpers (port of FullDataPointUtil)
# ---------------------------------------------------------------------------

def dp_get_id(data: int) -> int:
    """Extract the block/biome mapping ID from a data point."""
    return data & ID_MASK

def dp_get_height(data: int) -> int:
    """Extract the column height (in blocks) from a data point."""
    return (data >> HEIGHT_OFFSET) & HEIGHT_MASK

def dp_get_bottom_y(data: int) -> int:
    """Extract the relative bottom Y (relative to world min Y) from a data point."""
    return (data >> MIN_Y_OFFSET) & MIN_Y_MASK

def dp_get_block_light(data: int) -> int:
    return (data >> BLOCK_LIGHT_OFFSET) & BLOCK_LIGHT_MASK

def dp_get_sky_light(data: int) -> int:
    return (data >> SKY_LIGHT_OFFSET) & SKY_LIGHT_MASK


# ---------------------------------------------------------------------------
# Varint / zigzag (port of VarintUtil.java)
# ---------------------------------------------------------------------------

def read_varint(reader: BytesIO) -> int:
    """Read a protobuf-style unsigned varint from the stream."""
    value = 0
    shift = 0
    while True:
        b = reader.read(1)
        if not b:
            raise EOFError("Unexpected end of stream while reading varint")
        byte_val = b[0]
        value |= (byte_val & 0x7F) << shift
        shift += 7
        if shift >= 35:
            raise ValueError("Varint too long (exceeded 32 bits)")
        if (byte_val & 0x80) == 0:
            break
    return value


def zigzag_decode(n: int) -> int:
    """Decode a zigzag-encoded unsigned integer back to a signed integer."""
    return (n >> 1) ^ -(n & 1)


# ---------------------------------------------------------------------------
# Mapping blob deserialization (port of FullDataPointIdMap.deserialize)
# ---------------------------------------------------------------------------

BLOCK_STATE_SEPARATOR = "_DH-BSW_"

@dataclass
class MappingEntry:
    biome: str
    block_state: str
    full_string: str

    @property
    def lookup_key(self) -> str:
        """The block state string for color lookup."""
        return self.block_state


def deserialize_mapping(data: bytes) -> list[MappingEntry]:
    """
    Deserialize the Mapping blob into a list of MappingEntry objects.
    Each entry maps an integer ID to a biome + block state pair.
    """
    reader = BytesIO(data)
    count = struct.unpack(">i", reader.read(4))[0]
    if count < 0:
        raise ValueError(f"Invalid mapping count: {count}")

    entries: list[MappingEntry] = []
    for _ in range(count):
        length = struct.unpack(">H", reader.read(2))[0]
        raw_bytes = reader.read(length)
        if len(raw_bytes) != length:
            raise EOFError(f"Expected {length} bytes, got {len(raw_bytes)}")
        full_string = raw_bytes.decode("utf-8")

        sep_idx = full_string.find(BLOCK_STATE_SEPARATOR)
        if sep_idx == -1:
            raise ValueError(
                f"Mapping entry missing separator '{BLOCK_STATE_SEPARATOR}': {full_string}"
            )

        biome = full_string[:sep_idx]
        block_state = full_string[sep_idx + len(BLOCK_STATE_SEPARATOR):]
        entries.append(MappingEntry(biome=biome, block_state=block_state, full_string=full_string))

    return entries


# ---------------------------------------------------------------------------
# Data blob deserialization (port of FullDataSourceV2DTO)
# ---------------------------------------------------------------------------

@dataclass
class DataColumn:
    """A single vertical column of data points (one x,z position within a section)."""
    data_points: list[int] = field(default_factory=list)

    def __len__(self):
        return len(self.data_points)

    def __getitem__(self, index):
        return self.data_points[index]


@dataclass
class SectionData:
    """All decoded data for a single FullData row (one 64x64 section)."""
    detail_level: int
    pos_x: int
    pos_z: int
    columns: dict  # (rel_x, rel_z) -> DataColumn
    generation_steps: list[int] = field(default_factory=list)
    compression_modes: list[int] = field(default_factory=list)
    data_format_version: int = 0
    apply_to_parent: bool = False

    def get_world_block_x(self, rel_x: int) -> int:
        return self.pos_x * WIDTH + rel_x

    def get_world_block_z(self, rel_z: int) -> int:
        return self.pos_z * WIDTH + rel_z


def decompress_blob(data: bytes, compression_mode: int) -> bytes:
    """Decompress a blob using the specified compression mode."""
    if compression_mode == COMPRESS_UNCOMPRESSED:
        return data
    elif compression_mode == COMPRESS_ZSTD_BLOCK:
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)
    elif compression_mode == COMPRESS_LZ4:
        import lz4.frame
        return lz4.frame.decompress(data)
    elif compression_mode == COMPRESS_LZMA2:
        import lzma
        return lzma.decompress(data)
    elif compression_mode == COMPRESS_ZSTD_STREAM:
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
        reader = dctx.stream_reader(BytesIO(data))
        return reader.read()
    else:
        raise ValueError(f"Unknown compression mode: {compression_mode}")


def deserialize_data_blob_v2(
    data: bytes,
    columns: dict[tuple[int, int], DataColumn],
) -> None:
    """
    Deserialize a V2 data blob into the provided columns dict.

    V2 format (inner 62x62 region, border x/z=0 and x/z=63 excluded):
      1. Column counts  (varint each, 62*62 total)
      2. IDs + flags    (varint each): (id << 2) | has_light<<1 | has_discontinuity<<0
      3. Heights        (varint each)
      4. BottomY deltas (zigzag varint, only for mis-predicted entries)
      5. Packed light   (1 byte each, only for data points where has_light was set)

    All intermediate values are stored in separate arrays, then assembled
    into the final 64-bit data points at the end.
    """
    # Work with raw bytes + index for speed (avoids BytesIO overhead)
    buf = data
    pos = [0]  # mutable int for closure-like speed

    def read_varint_fast() -> int:
        p = pos[0]
        value = 0
        shift = 0
        while True:
            b = buf[p]
            p += 1
            value |= (b & 0x7F) << shift
            shift += 7
            if (b & 0x80) == 0:
                break
        pos[0] = p
        return value

    inner_min = 1
    inner_max = WIDTH - 1  # 63
    inner_size = inner_max - inner_min  # 62

    # --- Pass 1: Column counts ---
    column_counts: list[list[int]] = []
    for _ in range(inner_size):
        row = [read_varint_fast() for _ in range(inner_size)]
        column_counts.append(row)

    # --- Pass 2: IDs and flags ---
    dp_ids: list[list[list[int]]] = []
    dp_has_light: list[list[list[bool]]] = []
    dp_has_discontinuity: list[list[list[bool]]] = []

    for x in range(inner_size):
        ids_x, light_x, disc_x = [], [], []
        for z in range(inner_size):
            count = column_counts[x][z]
            ids_z, light_z, disc_z = [], [], []
            for _ in range(count):
                encoded = read_varint_fast()
                ids_z.append(encoded >> 2)
                light_z.append((encoded & 2) != 0)
                disc_z.append((encoded & 1) != 0)
            ids_x.append(ids_z)
            light_x.append(light_z)
            disc_x.append(disc_z)
        dp_ids.append(ids_x)
        dp_has_light.append(light_x)
        dp_has_discontinuity.append(disc_x)

    # --- Pass 3: Heights ---
    dp_heights: list[list[list[int]]] = []
    for x in range(inner_size):
        heights_x = []
        for z in range(inner_size):
            heights_z = [read_varint_fast() for _ in range(column_counts[x][z])]
            heights_x.append(heights_z)
        dp_heights.append(heights_x)

    # --- Pass 4: BottomY (predictive encoding, only mis-predicted entries stored) ---
    dp_bottom_ys: list[list[list[int]]] = []
    for x in range(inner_size):
        bottom_ys_x = []
        prev_by = 0
        for z in range(inner_size):
            count = column_counts[x][z]
            bottom_ys_z = []
            for y in range(count):
                height = dp_heights[x][z][y]
                if dp_has_discontinuity[x][z][y]:
                    error = zigzag_decode(read_varint_fast())
                    by = prev_by - height + error
                else:
                    by = prev_by - height
                bottom_ys_z.append(by)
                prev_by = by
            bottom_ys_x.append(bottom_ys_z)
        dp_bottom_ys.append(bottom_ys_x)

    # --- Pass 5: Packed light (only for data points with has_light) ---
    dp_sky_light: list[list[list[int]]] = []
    dp_block_light: list[list[list[int]]] = []
    for x in range(inner_size):
        sky_x, block_x = [], []
        for z in range(inner_size):
            count = column_counts[x][z]
            sky_z, block_z = [], []
            for y in range(count):
                if dp_has_light[x][z][y]:
                    packed = buf[pos[0]]
                    pos[0] += 1
                    sky_z.append(packed & 0xF)
                    block_z.append((packed >> 4) & 0xF)
                else:
                    sky_z.append(0)
                    block_z.append(0)
            sky_x.append(sky_z)
            block_x.append(block_z)
        dp_sky_light.append(sky_x)
        dp_block_light.append(block_x)

    # --- Assemble final 64-bit data points ---
    for x in range(inner_size):
        for z in range(inner_size):
            abs_x = inner_min + x
            abs_z = inner_min + z
            count = column_counts[x][z]
            col = DataColumn(data_points=[0] * count)

            for y in range(count):
                dp = (dp_ids[x][z][y] & ID_MASK) \
                   | ((dp_heights[x][z][y] & HEIGHT_MASK) << HEIGHT_OFFSET) \
                   | ((dp_bottom_ys[x][z][y] & MIN_Y_MASK) << MIN_Y_OFFSET) \
                   | ((dp_sky_light[x][z][y] & SKY_LIGHT_MASK) << SKY_LIGHT_OFFSET) \
                   | ((dp_block_light[x][z][y] & BLOCK_LIGHT_MASK) << BLOCK_LIGHT_OFFSET)
                col.data_points[y] = dp

            columns[(abs_x, abs_z)] = col


def deserialize_data_blob_v1(
    data: bytes,
    columns: dict[tuple[int, int], DataColumn],
) -> None:
    """
    Deserialize a V1 data blob into the provided columns dict.
    V1 format: For each of 64x64 columns:
      - short: column length
      - long[]: data points (raw 64-bit values)
    """
    reader = BytesIO(data)

    for rel_x in range(WIDTH):
        for rel_z in range(WIDTH):
            column_length = struct.unpack(">h", reader.read(2))[0]
            if column_length < 0:
                raise ValueError(f"Negative column length at ({rel_x}, {rel_z}): {column_length}")

            col = DataColumn(data_points=[])
            for _ in range(column_length):
                dp = struct.unpack(">q", reader.read(8))[0]
                col.data_points.append(dp)
            columns[(rel_x, rel_z)] = col


def deserialize_generation_steps(data: bytes) -> list[int]:
    """Deserialize the ColumnGenerationStep blob (64x64 bytes)."""
    if len(data) != WIDTH * WIDTH:
        raise ValueError(f"Generation steps blob size {len(data)} != {WIDTH * WIDTH}")
    return list(data)


def deserialize_compression_modes(data: bytes) -> list[int]:
    """Deserialize the ColumnWorldCompressionMode blob (64x64 bytes)."""
    if len(data) != WIDTH * WIDTH:
        raise ValueError(f"Compression modes blob size {len(data)} != {WIDTH * WIDTH}")
    return list(data)
