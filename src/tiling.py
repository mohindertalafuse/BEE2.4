"""Generates all tile brushes.

It also tracks overlays assigned to tiles, so we can regenerate all the brushes.
That allows any wall cube to be split into separate brushes, and make quarter-tile patterns.
"""
from collections import defaultdict, Counter

from enum import Enum
from typing import Tuple, Dict, List, Optional, Sequence, Union, Set

import instanceLocs
import vbsp_options
from srctools import Vec, Vec_tuple, VMF, Entity, Side, Solid
import srctools.logger
from brushLoc import POS as BLOCK_POS, Block
from texturing import TileSize, Portalable
import comp_consts as consts
import utils
import template_brush
import texturing
import antlines
import grid_optim

LOGGER = srctools.logger.get_logger(__name__)

# Face surfaces used to generate tiles.
# TILE_TEMP[tile_norm][u_norm, v_norm, thickness, is_bevel] = squarebeams_face
# thickness = 2,4,8
# TILE_TEMP[tile_norm]['tile'] = front_face
# TILE_TEMP[tile_norm]['back'] = back_face
TILE_TEMP = {}  # type: Dict[Tuple[float, float, float], Dict[Union[str, Tuple[int, int, int, bool]], Side]]

# Maps normals to the index in PrismFace.
PRISM_NORMALS = {
    # 0 = solid
    Vec.top: 1,
    Vec.bottom: 2,
    Vec.north: 3,
    Vec.south: 4,
    Vec.east: 5,
    Vec.west: 6,
}

NORMALS = [Vec(x=1), Vec(x=-1), Vec(y=1), Vec(y=-1), Vec(z=1), Vec(z=-1)]
# Specific angles, these ensure the textures align to world once done.
# IE upright on walls, up=north for floor and ceilings.
NORM_ANGLES = {
    Vec(x=1).as_tuple(): Vec(0, 0, 0),
    Vec(x=-1).as_tuple(): Vec(0, 180, 0),
    Vec(y=1).as_tuple(): Vec(0, 90, 0),
    Vec(y=-1).as_tuple(): Vec(0, 270, 0),
    Vec(z=1).as_tuple(): Vec(270, 270,  0),
    Vec(z=-1).as_tuple(): Vec(90, 90, 0),
}
# U-min, max, V-min, max in order.
UV_NORMALS = [(-1, 0), (1, 0), (0, -1), (0, 1)]

# All the tiledefs in the map.
# Maps a pos, normal -> tiledef
TILES = {}  # type: Dict[Tuple[Vec_tuple, Vec_tuple], TileDef]

# Special key for Tile.SubTile - This is set to 'u' or 'v' to
# indicate the center section should be nodrawed.
SUBTILE_FIZZ_KEY = object()

# Given the two bevel options, determine the correct texturing
# values.
# (min, max) -> (scale, offset)
BEVEL_BACK_SCALE = {
    (False, False): 128/512,  # |__|
    (False, True): 124/512,  # |__/
    (True, False): 124/512,  # \__|
    (True, True): 120/512,   # \__/
}


class TileType(Enum):
    """Physical types of geometry for each 1/4 tile."""
    WHITE = 0
    WHITE_4x4 = 1
    BLACK = 2
    BLACK_4x4 = 3
     
    NODRAW = 10  # Covered, so it should be set to nodraw

    # Air - used for embedFace sections.
    VOID = 11

    # 3 unit recess,  with backpanels or props/plastic behind. 
    # _BROKEN is ignored when allocating patterns - it wasn't there when the 
    #  tiles were installed. 
    # _PARTIAL is not, it's for WIP chambers.
    # If the skybox is 3D, _PARTIAL uses tools/skybox.
    CUTOUT_TILE_BROKEN = 22
    CUTOUT_TILE_PARTIAL = 23
    
    @property
    def is_recess(self):
        """Should this recess the surface?"""
        return self.value in (22, 23)
     
    @property   
    def is_nodraw(self):
        """Should this swap to nodraw?"""
        return self.value == 10
        
    @property
    def blocks_pattern(self):
        """Does this affect patterns?"""
        return self.value != 22
        
    @property
    def is_tile(self):
        """Is this a regular tile (white/black)."""
        return self.value < 10
        
    @property
    def is_white(self):
        """Is this portalable?"""
        return self.value in (0, 1)

    @property
    def color(self):
        """The portalability of the tile."""
        if self.value in (0, 1):
            return texturing.Portalable.WHITE
        elif self.value in (2, 3):
            return texturing.Portalable.BLACK
        raise ValueError('No colour for ' + self.name + '!')

    @property
    def inverted(self):
        """Swap the color of a type."""
        return _tiletype_inverted.get(self, self)

    @property
    def tile_size(self):
        """The size of the tile this should force."""
        if self.value in (1, 3):
            return TileSize.TILE_4x4
        else:
            return TileSize.TILE_1x1

    @staticmethod
    def with_color_and_size(size: TileSize, color: texturing.Portalable):
        """Return the TileType with a size and color."""
        return _tiletype_tiles[size, color]

_tiletype_tiles = {
    (TileSize.TILE_1x1, texturing.Portalable.BLACK): TileType.BLACK,
    (TileSize.TILE_1x1, texturing.Portalable.WHITE): TileType.WHITE,
    (TileSize.TILE_4x4, texturing.Portalable.BLACK): TileType.BLACK_4x4,
    (TileSize.TILE_4x4, texturing.Portalable.WHITE): TileType.WHITE_4x4,
}
_tiletype_inverted = {
    TileType.BLACK: TileType.WHITE,
    TileType.WHITE: TileType.BLACK,
    TileType.BLACK_4x4: TileType.WHITE_4x4,
    TileType.WHITE_4x4: TileType.BLACK_4x4,
}

# Symbols that represent TileSize values.
TILETYPE_TO_CHAR = {
    TileType.WHITE: 'W',
    TileType.WHITE_4x4: 'w',
    TileType.BLACK: 'B',
    TileType.BLACK_4x4: 'b',
    TileType.NODRAW: 'n',
    TileType.VOID: '.',
    TileType.CUTOUT_TILE_BROKEN: 'x',
    TileType.CUTOUT_TILE_PARTIAL: 'o',
}
TILETYPE_FROM_CHAR = {
    v: k
    for k, v in
    TILETYPE_TO_CHAR.items()
}  # type: Dict[str, TileType]


class BrushType(Enum):
    NORMAL = 0  # Normal surface.
    NODRAW = 1  # Nodraw brush, but needed to seal void and make backpanels.

    # Replaced by a template or off-grid embedFace. Shouldn't be modified by
    # us beyond retexturing and setting overlays.
    TEMPLATE = 2
    ANGLED_PANEL = 3  # Angled Panel - needs special handling for static versions.
    FLIP_PANEL = 4  # Flip panels - these are double-sided.


class PanelAngle(Enum):
    """Angles for static angled panels."""
    ANGLE_FLAT = 0  # Start disabled, so it's flat sticking out slightly.
    ANGLE_30 = 30
    ANGLE_45 = 45
    ANGLE_60 = 60
    ANGLE_90 = 90

    @classmethod
    def from_inst(cls, inst: Entity):
        """Get the angle desired for a panel."""
        if not inst.fixup.bool('$start_deployed'):
            return cls.ANGLE_FLAT
        # "ramp_90_deg_open" -> 90
        return cls(int(inst.fixup['$animation'][5:7]))


def round_grid(vec: Vec):
    """Round to the center of the grid."""
    return vec // 128 * 128 + (64, 64, 64)


def iter_uv(umin: float=0, umax: float=3, vmin: float=0, vmax: float=3):
    """Iterate over points in a rectangle."""
    urange = range(int(umin), int(umax + 1))
    vrange = range(int(vmin), int(vmax + 1))
    for u in urange:
        for v in vrange:
            yield u, v

TILE_SIZES = {
    TileSize.TILE_1x1: (4, 4),
    TileSize.TILE_2x1: (2, 4),
    TileSize.TILE_2x2: (2, 2),
    TileSize.TILE_4x4: (1, 1),
}


class Pattern:
    """Represents a position a tile can be positioned in."""
    def __init__(
        self,
        tex: TileSize,
        *tiles: Tuple[int, int, int, int],
        wall_only=False
    ):
        self.tex = tex
        self.wall_only = wall_only
        self.tiles = list(tiles)
        tile_u, tile_v = TILE_SIZES[tex]
        # Do some sanity checks on values..
        for umin, vmin, umax, vmax in tiles:
            tile_tex = '{} -> {} {} {} {}'.format(tex, umin, vmin, umax, vmax)
            assert 0 <= umin < umax <= 4, tile_tex
            assert 0 <= vmin < vmax <= 4, tile_tex
            assert (umax - umin) % tile_u == 0, tile_tex
            assert (vmax - vmin) % tile_v == 0, tile_tex
            
    def __repr__(self):
        return 'Pattern({!r}, {}{}'.format(
            self.tex,
            ','.join(map(repr, self.tiles)),
            ', wall_only=True)' if self.wall_only else ')'
        )


def order_bbox(bbox):
    """Used to sort 4x4 pattern positions.

    The pattern order is the order that they're tried in.
    We want to try the largest first so reverse the ordering used on max values.
    """
    umin, vmin, umax, vmax = bbox
    return umin, vmin, -umax, -vmax

PATTERNS = {
    'clean': [
        Pattern(TileSize.TILE_1x1, (0, 0, 4, 4)),
        Pattern(TileSize.TILE_2x1,
            (0, 0, 4, 4),  # Combined
            (0, 0, 2, 4), (2, 0, 4, 4),  # L/R
            (1, 0, 3, 4),  # Middle - only if no left or right.
            wall_only=True,
        ),
        Pattern(TileSize.TILE_2x2,
            # Combinations:
            (0, 0, 2, 4), (0, 0, 4, 2), (0, 2, 4, 4), (2, 0, 4, 4),
            (1, 0, 3, 4), (0, 1, 4, 3),

            (0, 0, 2, 2), (2, 0, 4, 2), (0, 2, 2, 4), (2, 2, 4, 4),  # Corners
            (0, 1, 4, 3),  # Special case - horizontal 2x1, don't use center.
            (1, 1, 3, 3),  # Center
            (1, 0, 3, 4), (1, 2, 3, 4),  # Vertical
            (0, 1, 2, 3), (2, 1, 4, 3),  # Horizontal
        ),

        # Combinations for 4x4, to merge adjacent blocks..
        Pattern(
            TileSize.TILE_4x4,
            *sorted([vals for x in range(4) for vals in [
                # V-direction, U-direction for each row/column.
                (x, 0, x+1, 4), (0, x, 4, x+1),  # 0-4
                (x, 2, x+1, 4), (2, x, 4, x+1),  # 2-4
                (x, 0, x+1, 2), (0, x, 2, x+1),  # 0-2
                (x, 1, x+1, 3), (1, x, 3, x+1),  # 1-3
                ]
            ], key=order_bbox)
        )
    ],

    # Don't have 2x2/1x1 tiles off-grid..
    'grid_only': [
        Pattern(TileSize.TILE_1x1, (0, 0, 4, 4)),
        Pattern(TileSize.TILE_2x1,
            (0, 0, 2, 4), (2, 0, 4, 4),  # L/R
            wall_only=True,
        ),
        Pattern(TileSize.TILE_2x2,
            (0, 0, 2, 2), (2, 0, 4, 2), (0, 2, 2, 4), (2, 2, 4, 4),  # Corners
        ),
    ],
    'fizzler_split_u': [],
    'fizzler_split_v': [],
}


def _make_patterns():
    """Set additional patterns which derive from CLEAN."""
    # These are the same as clean, but they don't allow any pattern
    # which crosses over the centerline in either direction.
    fizz_u = PATTERNS['fizzler_split_u']
    fizz_v = PATTERNS['fizzler_split_v']

    for pat in PATTERNS['clean']:
        pat_u = Pattern(pat.tex, wall_only=pat.wall_only)
        pat_v = Pattern(pat.tex, wall_only=pat.wall_only)
        for tile in pat.tiles:
            umin, vmin, umax, vmax = tile
            if umin >= 2 or umax <= 2:
                pat_u.tiles.append(tile)
            if vmin >= 2 or vmax <= 2:
                pat_v.tiles.append(tile)
        if pat_u.tiles:
            fizz_u.append(pat_u)
        if pat_v.tiles:
            fizz_v.append(pat_v)

_make_patterns()


class TileDef:
    """Represents one 128 block side.
    
    Attributes:
        pos: Vec for the center of the block.
        normal: The direction out of the block.
        brush_faces: A list of brush faces which this tiledef has exported.
          Empty before-hand, but after these are faces to attach antlines to.
        brush_type: BrushType - what sort of brush this is.
        base_type: TileSize this tile started with.
        override_tex: If set, a specific texture to use (skybox, light, backpanels etc)
        sub_tiles: None or a Dict[(u,v): TileSize]. u/v are either xz, yz or xy.
          If None or a point is not in the dict, it's the same as base_type. (None=1x1 tile).
        is_bullseye: If this tile has a bullseye attached to it (the instance is destroyed.)
        panel_inst: The instance for this panel, if it's a panel brush_type.
        panel_ent: The brush entity for the panel, if it's a panel brush_type.
    """
    __slots__ = [
        'pos',
        'normal',
        'brush_type',
        'brush_faces',
        'base_type',
        'sub_tiles',
        'override_tex',
        'is_bullseye',
        'panel_inst',
        'panel_ent',
        'extra_brushes',
    ]

    def __init__(
        self,
        pos: Vec, 
        normal: Vec,
        base_type: TileType,
        brush_type: BrushType=BrushType.NORMAL,
        subtiles: Dict[Tuple[int, int], TileType]=None,
        override_tex: str=None,
        is_bullseye: bool=False,
        panel_inst: Entity=None,
        panel_ent: Entity=None,
        extra_brushes: List[Solid]=(),
    ):
        self.pos = pos
        self.normal = normal
        self.brush_type = brush_type
        self.brush_faces = []  # type: List[Side]
        self.override_tex = override_tex
        self.base_type = base_type
        self.sub_tiles = subtiles
        self.is_bullseye = is_bullseye
        self.panel_inst = panel_inst
        self.panel_ent = panel_ent
        self.extra_brushes = list(extra_brushes)

    def __repr__(self):
        return '<{}, {} TileDef>'.format(
            self.base_type.name,
            self.brush_type.name,
        )

    def print_tiles(self):
        out = []
        for v in reversed(range(4)):
            for u in range(4):
                out.append(TILETYPE_TO_CHAR[self.sub_tiles[u, v]])
            out.append('\n')
        LOGGER.info('Subtiles: \n{}', ''.join(out))

    @classmethod
    def ensure(cls, grid_pos, norm, tile_type=TileType.VOID):
        """Return a tiledef at a position, creating it with a type if not present."""
        try:
            tile = TILES[grid_pos.as_tuple(), norm.as_tuple()]
        except KeyError:
            tile = TILES[grid_pos.as_tuple(), norm.as_tuple()] = cls(
                grid_pos,
                norm,
                tile_type,
            )
        tile.get_subtiles()
        return tile

    def get_subtiles(self) -> Dict[Tuple[int, int], TileType]:
        """Returns subtiles, creating it if not present."""
        if self.sub_tiles is None:
            self.sub_tiles = tile = {
                (x, y): self.base_type
                for x in range(4) for y in range(4)
            }
            return tile
        else:
            return self.sub_tiles

    def uv_offset(self, u, v, norm):
        """Return a u/v offset from our position.

        This is used for subtile orientations:
            norm is in the direction of the normal.
            If norm is x, uv = yz.
            If norm is y, uv = xz.
            If norm is z, uv = xy.
        """
        pos = self.pos.copy()
        u_ax, v_ax = Vec.INV_AXIS[self.normal.axis()]
        pos += self.normal * norm
        pos[u_ax] += u
        pos[v_ax] += v
        return pos

    def calc_patterns(
        self,
        tiles: Dict[Union[Tuple[int, int], object], TileType],
        is_wall=False,
        _pattern=None,
    ):
        """Figure out the brushes needed for a complex pattern.

        This returns
        """

        # copy it, so we can overwrite positions with VOID = not a tile.
        tiles = tiles.copy()

        # Don't check for special types if one is passed - that prevents
        # infinite recursion.
        if not _pattern:
            _pattern = 'clean'
            if SUBTILE_FIZZ_KEY in tiles:
                # Output the split patterns for centered fizzlers.
                # We need to remove it also so our iteration doesn't choke on it.
                # 'u' or 'v'
                split_type = tiles.pop(SUBTILE_FIZZ_KEY)  # type: str
                patterns = self.calc_patterns(
                    tiles,
                    is_wall,
                    'fizzler_split_' + split_type,
                )
                # Loop through our output and adjust the centerline outward.
                if split_type == 'u':
                    for umin, umax, vmin, vmax, grid_size, tile_type in patterns:
                        LOGGER.info('Pattern before: {}, {}: {}, {}', umin, vmin, umax, vmax)
                        if umin == 2:
                            umin = 2.5
                        if umax == 2:
                            umax = 1.5
                        LOGGER.info('Pattern after: {}, {}: {}, {}', umin, vmin, umax, vmax)
                        yield umin, umax, vmin, vmax, grid_size, tile_type
                    # Now yield the nodraw-brush.
                    yield 1.5, 2.5, 0, 4, TileSize.TILE_4x4, TileType.NODRAW
                elif split_type == 'v':
                    for umin, umax, vmin, vmax, grid_size, tile_type in patterns:
                        LOGGER.info('Pattern before: {}, {}: {}, {}', umin, vmin, umax, vmax)
                        if vmin == 2:
                            vmin = 2.5
                        if vmax == 2:
                            vmax = 1.5
                        LOGGER.info('Pattern after: {}, {}: {}, {}', umin, vmin, umax, vmax)
                        yield umin, umax, vmin, vmax, grid_size, tile_type
                    # Now yield the nodraw-brush.
                    yield 0, 4, 1.5, 2.5, TileSize.TILE_4x4, TileType.NODRAW
                return  # Don't run our checks on the tiles.

        for pattern in PATTERNS[_pattern]:
            if pattern.wall_only and not is_wall:
                continue
            for umin, vmin, umax, vmax in pattern.tiles:
                tile_type = tiles[umin, vmin]
                for uv in iter_uv(umin, umax-1, vmin, vmax-1):
                    if tiles[uv] is not tile_type:
                        break
                else:
                    for uv in iter_uv(umin, umax-1, vmin, vmax-1):
                        tiles[uv] = TileType.VOID
                    yield umin, umax, vmin, vmax, pattern.tex, tile_type

        # All unfilled spots are single 4x4 tiles, or other objects.
        for (u, v), tile_type in tiles.items():
            if tile_type is not TileType.VOID:
                yield u, u + 1, v, v + 1, TileSize.TILE_4x4, tile_type

    def export(self, vmf: VMF):
        """Create the solid for this."""
        bevels = [
            BLOCK_POS[
                'world': self.uv_offset(128*u, 128*v, 0)
            ].value not in (1, 2)
            for u, v in UV_NORMALS
        ]
        front_pos = self.pos + 64 * self.normal

        is_wall = bool(self.normal.z)

        if self.sub_tiles is None:
            full_type = self.base_type
        else:
            # If we have exactly 1 subtile, use a single full brush
            # since we know the pattern won't change.

            if len(set(self.sub_tiles.values())) == 1:
                full_type = next(iter(self.sub_tiles.values()))
            else:
                full_type = None

        # We only have one type of tile to generate.
        if full_type is not None:
            # If a 'normal' brush, we can simplify to just a single tile.
            # Otherwise go the full multitile route.
            if self.brush_type is BrushType.NORMAL:
                if full_type.is_nodraw:
                    tex = consts.Tools.NODRAW
                else:
                    tex = texturing.gen(
                        texturing.GenCat.NORMAL,
                        self.normal,
                        full_type.color,
                    ).get(front_pos, full_type.tile_size)
                brush, face = make_tile(
                    vmf,
                    self.pos + self.normal * 64,
                    self.normal,
                    top_surf=tex,
                    width=128,
                    height=128,
                    bevels=bevels,
                    back_surf=texturing.SPECIAL.get(self.pos, 'behind'),
                )
                self.brush_faces.append(face)
                yield brush
                return

        if self.sub_tiles is None:
            # Force subtiles to be all the parts we need.
            self.sub_tiles = dict.fromkeys(iter_uv(), full_type)

        # Multiple tile types in the block, or a special tiledef type - panels etc.

        if self.brush_type is BrushType.NORMAL:
            faces, brushes = self.gen_multitile_pattern(
                vmf,
                self.sub_tiles,
                is_wall,
                bevels,
                self.normal,
            )
            self.brush_faces.extend(faces)
            yield from brushes
        elif self.brush_type is BrushType.ANGLED_PANEL:
            if self.panel_inst.fixup.int('$connectioncount') > 0:
                # Dynamic panels are always beveled.
                bevels = (True, True, True, True)
                static_angle = None
            else:
                # Static panels can be straight.
                bevels = (False, False, False, False)
                static_angle = PanelAngle.from_inst(self.panel_inst)

            panel_angles = Vec.from_str(self.panel_inst['angles'])
            hinge_axis = Vec(y=1).rotate(*panel_angles)
            front_normal = Vec(x=1).rotate(*panel_angles)

            # For static 90 degree panels, we want to generate as if it's
            # in that position.
            if static_angle is PanelAngle.ANGLE_90:
                faces, brushes = self.gen_multitile_pattern(
                    vmf,
                    self.sub_tiles,
                    bool(front_normal.z),
                    bevels,
                    -front_normal,
                    vec_offset=64 * self.normal - 64 *front_normal,
                    thickness=2,
                )
            else:
                faces, brushes = self.gen_multitile_pattern(
                    vmf,
                    self.sub_tiles,
                    is_wall,
                    bevels,
                    self.normal,
                    offset=(64+8 if static_angle is PanelAngle.ANGLE_FLAT else 64),
                    thickness=(8 if static_angle is PanelAngle.ANGLE_FLAT else 2),
                )
            self.panel_ent.solids.extend(brushes)
            if static_angle is None or static_angle is PanelAngle.ANGLE_90:
                # Dynamic panel, do nothing.
                # 90 degree panels don't rotate either.
                pass
            elif static_angle is PanelAngle.ANGLE_FLAT:
                # Make it a func_detail.
                self.panel_ent.keys = {'classname': 'func_detail'}
                # Add nodraw behind to seal.
                brush, face = make_tile(
                    vmf,
                    self.pos + self.normal * 64,
                    self.normal,
                    top_surf=consts.Tools.NODRAW,
                    width=128,
                    height=128,
                    bevels=bevels,
                    back_surf=texturing.SPECIAL.get(self.pos, 'behind'),
                )
                vmf.add_brush(brush)
            else:
                self.panel_ent.keys = {'classname': 'func_detail'}

                # Rotate the panel to match the panel shape:
                # Figure out if we want to rotate +ve or -ve.
                # We know 90 degrees should rotate to the normal.
                if Vec(y=1).rotate(*hinge_axis.rotation_around()) == self.normal:
                    rotation = hinge_axis.rotation_around(static_angle.value)
                else:
                    rotation = hinge_axis.rotation_around(-static_angle.value)
                panel_offset = front_pos - 64 * front_normal
                for brush in brushes:
                    brush.localise(-panel_offset)
                    brush.localise(panel_offset, rotation)

        elif self.brush_type is BrushType.FLIP_PANEL:
            # Two surfaces, forward and backward - each is 4 thick.
            invert_black = self.panel_inst.fixup.bool('$start_reversed')
            inv_subtiles = {
                uv: (
                    tile_type.inverted
                    if invert_black or tile_type.color is Portalable.WHITE else
                    tile_type
                ) for uv, tile_type in self.sub_tiles.items()
            }
            front_faces, brushes = self.gen_multitile_pattern(
                vmf,
                self.sub_tiles,
                is_wall,
                (False, False, False, False),
                self.normal,
            )
            self.panel_ent.solids.extend(brushes)
            back_faces, brushes = self.gen_multitile_pattern(
                vmf,
                inv_subtiles,
                is_wall,
                (False, False, False, False),
                -self.normal,
                offset=64-8,
            )
            self.panel_ent.solids.extend(brushes)
            inset_flip_panel(self.panel_ent, front_pos, self.normal)

            # Allow altering the flip panel sounds.
            self.panel_ent['noise1'] = vbsp_options.get(str, 'flip_sound_start')
            self.panel_ent['noise2'] = vbsp_options.get(str, 'flip_sound_stop')

    def gen_multitile_pattern(
        self,
        vmf: VMF,
        pattern: Dict,
        is_wall: bool,
        bevels: Sequence[bool],
        normal: Vec,
        offset=64,
        thickness=4,
        vec_offset: Vec=None,
    ) -> Tuple[List[Side], List[Solid]]:
        """Generate a bunch of tiles, and return the front faces."""
        brushes = []
        faces = []

        # NOTE: calc_patterns can produce 0, 1, 1.5, 2, 2.5, 3, 4!
        # Half-values are for nodrawing fizzlers which are center-aligned.
        for umin, umax, vmin, vmax, grid_size, tile_type in self.calc_patterns(pattern, is_wall):
            # We bevel only the grid-edge tiles.
            tile_bevels = [
                umin == 0 and bevels[0],
                umax == 4 and bevels[1],
                vmin == 0 and bevels[2],
                vmax == 4 and bevels[3],
            ]
            tile_center = self.uv_offset(
                (umin + umax) * 16 - 64,
                (vmin + vmax) * 16 - 64,
                offset,
            )
            if vec_offset is not None:
                tile_center += vec_offset

            if tile_type.is_tile:
                u_size, v_size = TILE_SIZES[grid_size]
                tex = texturing.gen(
                    texturing.GenCat.NORMAL,
                    normal,
                    tile_type.color,
                ).get(tile_center, grid_size)
                brush, face = make_tile(
                    vmf,
                    tile_center,
                    normal,
                    top_surf=tex,
                    width=(umax - umin) * 32,
                    height=(vmax - vmin) * 32,
                    bevels=tile_bevels,
                    back_surf=texturing.SPECIAL.get(tile_center, 'behind'),
                    u_align=u_size * 128,
                    v_align=v_size * 128,
                    thickness=thickness,
                )
                faces.append(face)
                brushes.append(brush)
            elif tile_type is TileType.NODRAW:
                brush, face = make_tile(
                    vmf,
                    tile_center,
                    normal,
                    top_surf=consts.Tools.NODRAW,
                    width=(umax - umin) * 32,
                    height=(vmax - vmin) * 32,
                    bevels=bevels,
                    back_surf=texturing.SPECIAL.get(tile_center, 'behind'),
                )
                faces.append(face)
                brushes.append(brush)
        return faces, brushes


def edit_quarter_tile(
    origin: Vec,
    normal: Vec,
    tile_type: TileType,
    force=False,
):
    """Alter a 1/4 tile section of a tile."""
    norm_axis = normal.axis()
    u_axis, v_axis = Vec.INV_AXIS[norm_axis]

    grid_pos = round_grid(origin - normal)

    uv_pos = (origin - grid_pos + 64 - 16)
    u = uv_pos[u_axis] // 32 % 4
    v = uv_pos[v_axis] // 32 % 4

    if u != round(u) or v != round(v):
        return

    try:
        tile = TILES[grid_pos.as_tuple(), normal.as_tuple()]
    except KeyError:
        LOGGER.warning('Expected tile, but none found: {}, {}', origin, normal)
        return

    subtiles = tile.get_subtiles()

    old_tile = subtiles[u, v]

    if force:
        subtiles[u, v] = tile_type
        return

    # Don't replace void spaces with other things
    if old_tile is TileType.VOID:
        return

    # If nodrawed, don't revert for tiles.
    if old_tile is TileType.NODRAW:
        if tile_type.is_tile:
            return

    subtiles[u, v] = tile_type


def make_tile(
    vmf: VMF,
    origin: Vec, 
    normal: Vec, 
    top_surf: str,
    back_surf: str=consts.Tools.NODRAW.value,
    *,
    recess_dist=0,
    thickness=4,
    width=16,
    height=16,
    bevels=(False, False, False, False),
    panel_edge=False,
    u_align=512,
    v_align=512
) -> Tuple[Solid, Side]:
    """Generate a tile. 
    
    This uses UV coordinates, which equal xy, xz, or yz depending on normal.
    Parameters:
        * origin: Location of the center of the tile, on the block surface.
        * normal: Unit vector pointing out of the tile.
        * top_surf: Texture to apply to the front of the tile.
        * back_surf: Texture to apply to the back of the tile.
        * recess_dist: How far the front is below the block surface.
        * thickness: How far back the back surface is (normally 4). 2, 4, 8,
           Must be > recess_dist.
        * width: size in the U-direction. Must be > 8.
        * height: size in the V-direction. Must be > 8.
        * bevels: If that side should be 45° angled - in order, umin/max, vmin/max.
        * panel_edge: If True, use the panel-type squarebeams.
        * u_align: Wrap offsets to this much at maximum.
        * v_align: Wrap offsets to this much at maximum.
    """
    assert TILE_TEMP, "make_tile called without data loaded!"
    template = TILE_TEMP[normal.as_tuple()]

    assert width >= 8 and height >= 8, 'Tile is too small!' \
                                       ' ({}x{})'.format(width, height)
    assert thickness in (2, 4, 8), 'Bad thickness {}'.format(thickness)

    axis_u, axis_v = Vec.INV_AXIS[normal.axis()]

    top_side = template['front'].copy(vmf_file=vmf)
    top_side.mat = top_surf
    top_side.translate(origin - recess_dist * normal)

    block_min = round_grid(origin) - (64, 64, 64)

    top_side.uaxis.offset = 4 * (
        block_min[axis_u] - (origin[axis_u] - width/2)
    ) % u_align
    top_side.vaxis.offset = 4 * (
        block_min[axis_v] - (origin[axis_v] - height/2)
    ) % v_align

    bevel_umin, bevel_umax, bevel_vmin, bevel_vmax = bevels

    back_side = template['back'].copy(vmf_file=vmf)  # type: Side
    back_side.mat = back_surf
    # The offset was set to zero in the original we copy from.
    back_side.uaxis.scale = BEVEL_BACK_SCALE[bevel_umin, bevel_umax]
    back_side.vaxis.scale = BEVEL_BACK_SCALE[bevel_vmin, bevel_vmax]
    # Shift the surface such that it's aligned to the minimum edge.
    back_side.translate(origin - normal * thickness + Vec.with_axes(
        axis_u, 4 * bevel_umin - 64,
        axis_v, 4 * bevel_vmin - 64,
    ))

    umin_side = template[-1, 0, thickness, bevel_umin].copy(vmf_file=vmf)
    umin_side.translate(origin + Vec.with_axes(axis_u, -width/2))

    umax_side = template[1, 0, thickness, bevel_umax].copy(vmf_file=vmf)
    umax_side.translate(origin + Vec.with_axes(axis_u, width/2))

    vmin_side = template[0, -1, thickness, bevel_vmin].copy(vmf_file=vmf)
    vmin_side.translate(origin + Vec.with_axes(axis_v, -height/2))

    vmax_side = template[0, 1, thickness, bevel_vmax].copy(vmf_file=vmf)
    vmax_side.translate(origin + Vec.with_axes(axis_v, height/2))

    for face in [umin_side, umax_side, vmin_side, vmax_side]:
        face.uaxis.offset %= 512
        face.vaxis.offset = 0

    back_side.uaxis.offset %= 512
    back_side.vaxis.offset %= 512

    edge_name = 'panel_edge' if panel_edge else 'edge'

    umin_side.mat = texturing.SPECIAL.get(origin, edge_name)
    umax_side.mat = texturing.SPECIAL.get(origin, edge_name)
    vmin_side.mat = texturing.SPECIAL.get(origin, edge_name)
    vmax_side.mat = texturing.SPECIAL.get(origin, edge_name)

    return Solid(vmf, sides=[
        top_side, back_side,
        umin_side, umax_side,
        vmin_side, vmax_side,
    ]), top_side


def gen_tile_temp():
    """Generate the sides used to create tiles.

    This populates TILE_TEMP with pre-rotated solids in each direction,
     with each side identified.
    """

    categories = {
        (2, True): 'bevel_thin',
        (4, True): 'bevel_norm',
        (8, True): 'bevel_thick',

        (2, False): 'flat_thin',
        (4, False): 'flat_norm',
        (8, False): 'flat_thick',
    }  # type: Dict[Tuple[int, bool], Solid]

    try:
        template = template_brush.get_template('__TILING_TEMPLATE__')
        # Template -> world -> first solid
        for (key, name) in categories.items():
            categories[key] = template.visgrouped(name)[0][0]
    except KeyError:
        raise Exception('Bad Tiling Template!')

    for norm_tup, angles in NORM_ANGLES.items():
        norm = Vec(norm_tup)
        axis_norm = norm.axis()

        TILE_TEMP[norm_tup] = temp_part = {}

        for ((thickness, bevel), temp) in categories.items():
            brush = temp.copy()
            brush.localise(Vec(), angles)

            for face in brush:
                if face.mat == consts.Special.BACKPANELS:
                    # Only copy the front and back from the normal template.
                    if thickness == 4 and not bevel:
                        temp_part['back'] = face
                        face.translate(2 * norm)
                        # Set it to zero here, so we don't need to reset
                        # it in make_tile.
                        face.offset = 0
                elif face.mat in consts.BlackPan or face.mat in consts.WhitePan:
                    if thickness == 4 and not bevel:
                        temp_part['front'] = face
                        face.translate(-2 * norm)
                else:
                    # Squarebeams
                    face_norm = round(face.get_origin().norm())  # type: Vec
                    face.translate(-16 * face_norm - (thickness/ 2) * norm)
                    u_dir, v_dir = face_norm.other_axes(axis_norm)
                    temp_part[u_dir, v_dir, thickness, bevel] = face


def analyse_map(vmf_file: VMF, side_to_ant_seg: Dict[str, List[antlines.Segment]]):
    """Create TileDefs from all the brush sides.

    Once done, all wall brushes have been removed from the map.
    """

    # Face ID -> tileDef, used to match overlays to their face targets.
    # Invalid after we exit, since all the IDs have been freed and may be
    # reused later.
    face_to_tile = {}  # type: Dict[str, TileDef]

    for brush in vmf_file.brushes[:]:
        bbox_min, bbox_max = brush.get_bbox()
        dim = bbox_max - bbox_min
        grid_pos = round_grid(bbox_min)
        if dim == (128, 128, 128):
            tiledefs_from_cube(face_to_tile, brush, grid_pos)
            continue

        norm = Vec()
        for axis in 'xyz':
            if dim[axis] == 4:
                norm[axis] = (-1 if bbox_min[axis] - grid_pos[axis] < 0 else 1)
                break
        else:
            # Has no 4-unit side - not a PeTI brush?
            LOGGER.warning('Unrecognised brush from {} to {}'.format(bbox_min, bbox_max))
            continue

        tile_size = dim.other_axes(norm.axis())
        if tile_size == (128, 128):
            # 128x128x4 block..
            tiledefs_from_large_tile(face_to_tile, brush, grid_pos, norm)
        else:
            # EmbedFace block..
            tiledefs_from_embedface(face_to_tile, brush, grid_pos, norm)

    # Look for Angled panels.
    # First grab the instances.
    panel_inst = instanceLocs.resolve('<ITEM_PANEL_ANGLED>, <ITEM_PANEL_FLIP>')

    panels = {}  # type: Dict[str, Entity]

    for inst in vmf_file.by_class['func_instance']:
        if inst['file'].casefold() in panel_inst:
            panels[inst['targetname']] = inst

    dynamic_pan_parent = vbsp_options.get(str, "dynamic_pan_parent")
    import conditions

    for brush_ent in vmf_file.by_class['func_brush']:
        # Grab the instance name out of the parent - these are the
        # only ones with parents in default PeTI.
        if brush_ent['parentname']:
            # Strip '-model_arms'...
            panel_inst = panels[brush_ent['parentname'][:-11]]
            brush_ent['parentname'] = conditions.local_name(
                panel_inst,
                dynamic_pan_parent
            )
            tiledef_from_angled_panel(brush_ent, panel_inst)

    for brush_ent in vmf_file.by_class['func_door_rotating']:
        # Strip '-flipping_panel'...
        panel_inst = panels[brush_ent['targetname'][:-15]]
        tiledef_from_flip_panel(brush_ent, panel_inst)

    # Tell the antlines which tiledefs they attach to.
    for side, segments in side_to_ant_seg.items():
        try:
            tile = face_to_tile[side]
        except KeyError:
            continue
        for seg in segments:
            seg.tiles.append(tile)

    # Parse face IDs saved in overlays - if they're matching a tiledef,
    # remove them.
    for over in vmf_file.by_class['info_overlay']:
        faces = over['sides', ''].split(' ')
        tiles = over.tiledefs = []
        for face in faces[:]:
            try:
                tiles.append(face_to_tile[int(face)])
            except (KeyError, ValueError):
                pass
            else:
                faces.remove(face)
        over['sides'] = ' '.join(faces)


def tiledefs_from_cube(face_to_tile, brush: Solid, grid_pos: Vec):
    """Generate a tiledef matching a 128^3 block."""
    for face in brush:
        normal = -face.normal()
        special_tex = None

        # These cubes don't contain any items, so it's fine
        # if we get rid of sides that aren't useful.
        # if it's bordering void or another solid, it's unneeded.
        neighbour_block = BLOCK_POS['world': grid_pos + 128 * normal]
        if not neighbour_block.traversable:
            continue

        if face.mat in consts.BlackPan:
            tex_kind = TileType.BLACK
        elif face.mat in consts.WhitePan:
            tex_kind = TileType.WHITE
        else:
            tex_kind = TileType.BLACK
            special_tex = face.mat

        tiledef = TileDef(
            grid_pos,
            normal,
            base_type=tex_kind,
            override_tex=special_tex,
        )
        TILES[grid_pos.as_tuple(), normal.as_tuple()] = tiledef
        face_to_tile[face.id] = tiledef
    brush.remove()


def tiledefs_from_large_tile(face_to_tile, brush: Solid, grid_pos: Vec, norm: Vec):
    """Generate a tiledef matching a 128x128x4 side."""
    tex_kind, special_tex, front_face = find_front_face(brush, grid_pos, norm)

    neighbour_block = BLOCK_POS['world': grid_pos + 128 * norm]

    if neighbour_block is Block.VOID:
        tex_kind = TileType.NODRAW

    tiledef = TileDef(
        grid_pos,
        norm,
        base_type=tex_kind,
        override_tex=special_tex,
    )
    TILES[grid_pos.as_tuple(), norm.as_tuple()] = tiledef
    brush.map.remove_brush(brush)
    face_to_tile[front_face.id] = tiledef


def tiledef_from_angled_panel(brush_ent: Entity, panel_ent: Entity):
    """Generate a tiledef matching an angled panel."""
    brush = brush_ent.solids.pop()
    assert not brush_ent.solids, 'Multiple brushes in angled panel?'

    grid_pos = round_grid(Vec.from_str(panel_ent['origin']))
    norm = Vec(z=1).rotate_by_str(panel_ent['angles'])
    grid_pos -= 128*norm

    tex_kind, special_tex, front_face = find_front_face(brush, grid_pos, norm)

    TILES[grid_pos.as_tuple(), norm.as_tuple()] = TileDef(
        grid_pos,
        norm,
        base_type=tex_kind,
        brush_type=BrushType.ANGLED_PANEL,
        override_tex=special_tex,
        panel_ent=brush_ent,
        panel_inst=panel_ent,
    )


def tiledef_from_flip_panel(brush_ent: Entity, panel_ent: Entity):
    """Generate a tiledef matching a flip panel."""
    brush_ent.solids.clear()
    grid_pos = round_grid(Vec.from_str(panel_ent['origin']))
    norm = Vec(z=1).rotate_by_str(panel_ent['angles'])
    grid_pos -= 128*norm

    TILES[grid_pos.as_tuple(), norm.as_tuple()] = TileDef(
        grid_pos,
        norm,
        # It's always white in the forward direction
        base_type=TileType.WHITE,
        brush_type=BrushType.FLIP_PANEL,
        panel_ent=brush_ent,
        panel_inst=panel_ent,
    )


def tiledefs_from_embedface(
    face_to_tile,
    brush: Solid,
    grid_pos: Vec,
    norm: Vec,
):
    """Generate a tiledef matching EmbedFace brushes."""

    tex_kind, special_tex, front_face = find_front_face(brush, grid_pos, norm)

    norm_axis = norm.axis()

    bbox_min, bbox_max = brush.get_bbox()
    bbox_min[norm_axis] = bbox_max[norm_axis] = 0
    if bbox_min % 32 or bbox_max % 32 or special_tex is not None:
        # Not aligned to grid, leave this here!
        return

    tile = TileDef.ensure(grid_pos, norm)
    face_to_tile[front_face.id] = tile
    brush.remove()

    grid_min = grid_pos - (64, 64, 64)
    u_min, v_min = (bbox_min - grid_min).other_axes(norm_axis)
    u_max, v_max = (bbox_max - grid_min).other_axes(norm_axis)

    u_min, u_max = u_min // 32, u_max // 32 - 1
    v_min, v_max = v_min // 32, v_max // 32 - 1
    for uv in iter_uv(u_min, u_max, v_min, v_max):
        tile.sub_tiles[uv] = tex_kind


def find_front_face(
    brush: Solid,
    grid_pos: Vec,
    norm: Vec,
) -> Tuple[TileType, Optional[str], Side]:
    """Find the tile face in a brush. Returns color, special_mat, face."""
    for face in brush:
        if -face.normal() != norm:
            continue
        if face.mat in consts.BlackPan:
            return TileType.BLACK, None, face
        elif face.mat in consts.WhitePan:
            return TileType.WHITE, None, face
        else:
            return TileType.BLACK, face.mat, face
    else:
        raise Exception('Malformed wall brush at {}, {}'.format(grid_pos, norm))


def inset_flip_panel(panel: Entity, pos: Vec, normal: Vec):
    """Inset the sides of a flip panel, to not hit the borders."""
    norm_axis = normal.axis()
    for side in panel.sides():
        norm = side.normal()
        if norm.axis() == norm_axis:
            continue  # Front or back

        u_off, v_off = (side.get_origin() - pos).other_axes(norm_axis)
        if abs(u_off) == 64 or abs(v_off) == 64:
            side.translate(2 * norm)
            # Snap squarebeams to each other.
            side.vaxis.offset = 0


def generate_brushes(vmf: VMF):
    """Generate all the brushes in the map, then set overlay sides."""
    for tile in TILES.values():
        brushes = list(tile.export(vmf))
        vmf.add_brushes(brushes)

    for over in vmf.by_class['info_overlay']:
        try:
            tiles = over.tiledefs  # type: List[TileDef]
        except AttributeError:
            continue
        faces = over['sides', ''].split(' ')
        for tile in tiles:
            faces.extend(str(f.id) for f in tile.brush_faces)

        if faces:
            over['sides'] = ' '.join(faces)
        else:
            over.remove()

    LOGGER.info('Generating goop...')
    generate_goo(vmf)


def generate_goo(vmf: VMF):
    """Generate goo pit brushes and triggers."""
    # We want to use as few brushes as possible.
    # So group them by their min/max Z, and then produce bounding boxes.

    goo_pos = defaultdict(dict)  # type: Dict[Tuple[int, int], Dict[Tuple[int, int], bool]]

    # Calculate the z-level with the largest number of goo brushes,
    # so we can ensure the 'fancy' pit is the largest one.
    # Valve just does it semi-randomly.
    goo_heights = Counter()

    for pos, block_type in BLOCK_POS.items():
        if block_type is Block.GOO_SINGLE:
            goo_pos[pos.z, pos.z][pos.x, pos.y] = True

            goo_heights[pos.as_tuple()] += 1
        elif block_type is Block.GOO_TOP:
            goo_heights[pos.as_tuple()] += 1
            # Multi-layer..
            lower_pos = BLOCK_POS.raycast(pos, Vec(0, 0, -1))

            goo_pos[lower_pos.z, pos.z][pos.x, pos.y] = True

    LOGGER.info('Goo pos: {}', goo_pos)

    # No goo.
    if not goo_pos:
        return

    goo_scale = vbsp_options.get(float, 'goo_scale')

    # Find key with the highest value - that gives the largest z-level.
    best_goo = max(goo_heights.items(), key=lambda x: x[1])[0]

    LOGGER.info('Goo heights: {} <- {}', best_goo, goo_heights)

    for ((min_z, max_z), grid) in goo_pos.items():
        for min_x, min_y, max_x, max_y in grid_optim.optimise(grid):
            bbox_min = Vec(min_x, min_y, min_z) * 128
            bbox_max = Vec(max_x, max_y, max_z) * 128
            prism = vmf.make_prism(
                bbox_min,
                bbox_max + (128, 128, 96),
            )
            # Apply goo scaling
            prism.top.scale = goo_scale
            # Use fancy goo on the level with the
            # highest number of blocks.
            # All plane z are the same.
            prism.top.mat = texturing.SPECIAL.get(
                bbox_max + (0, 0, 96), (
                    'goo' if
                    bbox_max.z == best_goo
                    else 'goo_cheap'
                ),
            )
            vmf.add_brush(prism.solid)
