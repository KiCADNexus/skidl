# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) 2016-2021 Dave Vandenbout.

"""
Handler for reading Kicad libraries and generating netlists.
"""

from __future__ import (  # isort:skip
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import os.path
import time
from builtins import range, str
import os
import re
from builtins import int, range, zip
from collections import Counter, namedtuple

from future import standard_library

from skidl.pckg_info import __version__
from skidl.scriptinfo import get_script_name, scriptinfo
from skidl.utilities import add_quotes, export_to_all, find_and_open_file, rmv_attr
from skidl.logger import active_logger
from skidl.part import LIBRARY
from skidl.schematics.geometry import (
    Tx,
    BBox,
    Point,
    Vector,
    tx_rot_0,
    tx_rot_90,
    tx_rot_180,
    tx_rot_270,
)
from skidl.utilities import export_to_all, find_and_read_file, num_to_chars, rmv_quotes
from .constants import HIER_TERM_SIZE, PIN_LABEL_FONT_SIZE
from skidl.schematics.geometry import BBox, Point, Tx, Vector
from skidl.schematics.place import PlacementFailure
from skidl.schematics.route import RoutingFailure
from skidl.schematics.node import Node
from skidl.schematics.net_terminal import NetTerminal
from .gen_schematic import node_to_eeschema
from .draw_objs import *

standard_library.install_aliases()


# These aren't used here, but they are used in modules
# that include this module.
tool_name = "kicad"
lib_suffix = [".lib"]

__all__ = ["tool_name", "lib_suffix"]


@export_to_all
def get_kicad_lib_tbl_dir():
    """Get the path to where the global fp-lib-table file is found."""

    paths = (
        "$HOME/.config/kicad",
        "~/.config/kicad",
        "%APPDATA%/kicad",
        "$HOME/Library/Preferences/kicad",
        "~/Library/Preferences/kicad",
    )

    for path in paths:
        path = os.path.normpath(os.path.expanduser(os.path.expandvars(path)))
        if os.path.lexists(path):
            return path
    return ""


@export_to_all
def load_sch_lib(lib, filename=None, lib_search_paths_=None, lib_section=None):
    """
    Load the parts from a KiCad schematic library file.

    Args:
        lib (SchLib): SKiDL library object.
        filename (str): The name of the KiCad schematic library file.
        lib_search_paths_ (list): List of paths with KiCad symbol libraries.
        lib_section: Only used for SPICE simulations.
    """

    from skidl import lib_suffixes, Part, KICAD

    # Try to open the file using allowable suffixes for the versions of KiCAD.
    suffixes = lib_suffixes[KICAD]
    base, suffix = os.path.splitext(filename)
    if suffix:
        # If an explicit file extension was given, use it instead of tool lib default extensions.
        suffixes = [suffix]
    for suffix in suffixes:
        # Allow file open failure so multiple suffixes can be tried without error messages.
        f, _ = find_and_open_file(
            filename, lib_search_paths_, suffix, allow_failure=True
        )
        if f:
            # Break from the loop once a library file is successfully opened.
            break
    if not f:
        raise FileNotFoundError(
            "Unable to open KiCad Schematic Library File {}".format(filename)
        )

    lib_txt = f.read()
    try:
        lib_txt = lib_txt.decode("latin_1")
    except AttributeError:
        # File contents were already decoded.
        pass

    part_defns = lib_txt.split("\nDEF ")

    # Check the file header to make sure it's a KiCad library.
    header = part_defns.pop(0)  # Stuff before first DEF is the header.
    if not header.startswith("EESchema-LIBRARY"):
        active_logger.raise_(
            RuntimeError,
            "The file {} is not a KiCad Schematic Library File.\n".format(filename),
        )

    # Process each part.
    for part_defn in part_defns:

        part_defn = part_defn.split("\n")
        part_defn[0] = "DEF " + part_defn[0]  # Add DEF back onto first line.

        # Remove comments.
        # part_defn = re.sub("(\n?)([^#]*?)#[^#]*?\n", r"\1\2", part_defn)
        part_defn = [line for line in part_defn if not line.startswith("#")]

        # Get part name.
        part_name = part_defn[0].split()[1]

        # Get part aliases between "ALIAS " and newline.
        aliases = []
        for line in part_defn:
            if line.startswith("ALIAS "):
                aliases = line.split()[1:]
                break

        # Create the Part object and add it to the part list. Be sure to
        # indicate that the Part object is being added to a library
        # and not to a schematic netlist.
        # Also, add null attributes in case a DCM file is not
        # available for this part.
        lib.add_parts(
            Part(
                part_defn=part_defn,
                tool=KICAD,
                dest=LIBRARY,
                filename=filename,
                name=part_name,
                aliases=aliases,
                keywords="",
                datasheet="",
                description="",
                search_text="",
                tool_version="kicad",
            )
        )

    # Now add information from any associated DCM file.
    base_fn = os.path.splitext(filename)[0]  # Strip any extension.
    dcm_txt, _ = find_and_read_file(
        base_fn, lib_search_paths_, ".dcm", allow_failure=True
    )
    if dcm_txt:
        part_desc = {}

        for line in dcm_txt.split("\n"):
            # for line in f.read().split("\n"):

            # Skip over comments.
            if line.startswith("#"):
                pass

            # Look for the start of a part description.
            elif line.startswith("$CMP"):
                part_desc["name"] = line.split()[-1]

            # If gathering the part description has begun, then continue adding lines.
            elif part_desc:
                if line.startswith("D"):
                    part_desc["description"] = " ".join(line.split()[1:])
                elif line.startswith("K"):
                    part_desc["keywords"] = " ".join(line.split()[1:])
                elif line.startswith("F"):
                    part_desc["datasheet"] = " ".join(line.split()[1:])
                elif line.startswith("$ENDCMP"):
                    # Part description complete, so store it in the part(s) with matching name.
                    for part in lib.get_parts_quick(part_desc["name"]):
                        part.description = part_desc.get("description", "")
                        part.keywords = part_desc.get("keywords", "")
                        part.datasheet = part_desc.get("datasheet", "")
                    part_desc = {}
                else:
                    pass

    # Create text string to be used when searching for parts.
    for part in lib.parts:
        search_text_pieces = [part.filename, part.description, part.keywords]
        search_text_pieces.extend(part.aliases)  # aliases also includes part name.
        # Join the various text pieces by newlines so the ^ and $ special characters
        # can be used to detect the start and end of a piece of text during RE searches.
        part.search_text = "\n".join(search_text_pieces)


@export_to_all
def parse_lib_part(self, partial_parse):
    """
    Create a Part using a part definition from a KiCad schematic library.

    This method was written based on the code from
    https://github.com/KiCad/kicad-library-utils/tree/master/schlib.
    It's covered by GPL3.

    Args:
        partial_parse: If true, scan the part definition until the
            name and aliases are found. The rest of the definition
            will be parsed if the part is actually used.
    """

    from skidl import Pin

    _DEF_KEYS = [
        "name",
        "reference",
        "unused",
        "text_offset",
        "draw_pinnumber",
        "draw_pinname",
        "unit_count",
        "units_locked",
        "option_flag",
    ]
    _F0_KEYS = [
        "reference",
        "posx",
        "posy",
        "text_size",
        "text_orient",
        "visibility",
        "htext_justify",
        "vtext_justify",
    ]
    _FN_KEYS = [
        "name",
        "posx",
        "posy",
        "text_size",
        "text_orient",
        "visibility",
        "htext_justify",
        "vtext_justify",
        "fieldname",
    ]
    _ARC_KEYS = [
        "posx",
        "posy",
        "radius",
        "start_angle",
        "end_angle",
        "unit",
        "convert",
        "thickness",
        "fill",
        "startx",
        "starty",
        "endx",
        "endy",
    ]
    _CIRCLE_KEYS = ["posx", "posy", "radius", "unit", "convert", "thickness", "fill"]
    _POLY_KEYS = ["point_count", "unit", "convert", "thickness", "points", "fill"]
    _RECT_KEYS = [
        "startx",
        "starty",
        "endx",
        "endy",
        "unit",
        "convert",
        "thickness",
        "fill",
    ]
    _TEXT_KEYS = [
        "direction",
        "posx",
        "posy",
        "text_size",
        "text_type",
        "unit",
        "convert",
        "text",
        "italic",
        "bold",
        "hjustify",
        "vjustify",
    ]
    _PIN_KEYS = [
        "name",
        "num",
        "posx",
        "posy",
        "length",
        "direction",
        "name_text_size",
        "num_text_size",
        "unit",
        "convert",
        "electrical_type",
        "pin_type",
    ]
    _DRAW_KEYS = {
        "arcs": _ARC_KEYS,
        "circles": _CIRCLE_KEYS,
        "polylines": _POLY_KEYS,
        "rectangles": _RECT_KEYS,
        "texts": _TEXT_KEYS,
        "pins": _PIN_KEYS,
    }
    _DRAW_ELEMS = {
        "arcs": "A",
        "circles": "C",
        "polylines": "P",
        "rectangles": "S",
        "texts": "T",
        "pins": "X",
    }
    _KEYS = {
        "DEF": _DEF_KEYS,
        "F0": _F0_KEYS,
        "F": _FN_KEYS,
        "A": _ARC_KEYS,
        "C": _CIRCLE_KEYS,
        "P": _POLY_KEYS,
        "S": _RECT_KEYS,
        "T": _TEXT_KEYS,
        "X": _PIN_KEYS,
    }

    def numberize(v):
        """If possible, convert a string into a number."""
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                pass
        return v  # Unable to convert to number. Return string.

    # Return if there's nothing to do (i.e., part has already been parsed).
    if not self.part_defn:
        return

    self.aliases = []  # Part aliases.
    self.fplist = []  # Footprint list.
    self.draw = []  # Drawing commands for symbol, including pins.

    building_fplist = False  # True when working on footprint list in defn.
    building_draw = False  # True when gathering part drawing from defn.

    pins = {}  # Dict of symbol pins to check for duplicates.

    # Regular expression for non-quoted and quoted text pieces.
    unqu = r'[^\s"]+'  # Word without spaces or double-quotes.
    qu = r'(?<!\\)".*?(?<!\\)"'  # Quoted string, possibly with escaped quotes.
    srch = "|".join([unqu + qu, qu, unqu])
    srch = re.compile(srch)

    # Go through the part definition line-by-line.
    for line in self.part_defn:

        # Split the line into words.
        line = line.replace("\n", "")

        # Extract all the non-quoted and quoted text pieces, accounting for escaped quotes.
        line = re.findall(srch, line)  # Replace line with list of pieces of line.

        # The first word indicates the type of part definition data that will follow.
        if line[0] in _KEYS:
            # Get the keywords for the current part definition data.
            key_list = _KEYS[line[0]]
            # Make a list of the values in the part data associated with each key.
            # Use an empty string for any missing values so every key will be
            # associated with something.
            values = line[1:] + ["" for _ in range(len(key_list) - len(line[1:]))]
            values = [rmv_quotes(v) for v in values]  # Remove any quotes from values.

        # Create a dictionary of part definition keywords and values.
        if line[0] == "DEF":
            self.definition = dict(list(zip(_DEF_KEYS, values)))
            self.name = self.definition["name"]

            # To handle libraries quickly, just get the name and
            # aliases and parse the rest of the part definition later.
            if partial_parse:
                if self.aliases:
                    # Name found, aliases already found so we're done.
                    return
                # Name found so scan defn to see if aliases are present.
                # (The majority of parts don't have aliases.)
                for ln in self.part_defn:
                    if re.match(r"^\s*ALIAS\s", ln):
                        # Found aliases, so store them.
                        self.aliases = re.findall(srch, ln)[1:]
                        return
                return

            # Add DEF field to list of things to draw.
            values = [numberize(v) for v in values]
            self.draw.append(DrawDef(*values))

        # End the parsing of the part definition.
        elif line[0] == "ENDDEF":
            break

        # Create a dictionary of F0 part field keywords and values.
        elif line[0] == "F0":
            field_dict = dict(list(zip(_F0_KEYS, values)))
            # Add the field name and its value as an attribute to the part.
            self.fields["F0"] = field_dict["reference"]
            # Add F0 field to list of things to draw.
            values = [numberize(v) for v in values]
            self.draw.append(DrawF0(*values))

        # Create a dictionary of the other part field keywords and values.
        elif line[0][0] == "F":
            # Make a list of field values with empty strings for missing fields.
            values = line[1:] + ["" for _ in range(len(_FN_KEYS) - len(line[1:]))]
            values = [rmv_quotes(v) for v in values]  # Remove any quotes from values.
            field_dict = dict(list(zip(_FN_KEYS, values)))
            # If no field name at end of line, use the field identifier F1, F2, ...
            field_dict["fieldname"] = field_dict["fieldname"] or line[0]
            # Add the field name and its value as an attribute to the part.
            self.fields[field_dict["fieldname"]] = field_dict["name"]
            # Add F1 field to list of things to draw.
            if line[0] == "F1":
                values = [numberize(v) for v in values]
                self.draw.append(DrawF1(*values))

        # Create a list of part aliases.
        elif line[0] == "ALIAS":
            self.aliases = line[1:]

        # Start the list of part footprints.
        elif line[0] == "$FPLIST":
            building_fplist = True

        # End the list of part footprints.
        elif line[0] == "$ENDFPLIST":
            building_fplist = False

        # Start gathering the drawing primitives for the part symbol.
        elif line[0] == "DRAW":
            building_draw = True

        # End the gathering of drawing primitives.
        elif line[0] == "ENDDRAW":
            building_draw = False

        # Every other line is either a footprint or a drawing primitive.
        else:
            # If the footprint list is being built, then add this line to it.
            if building_fplist:
                self.fplist.append(
                    line[0].strip().rstrip()
                )  # Remove begin & end whitespace.

            # Else if the drawing primitives are being gathered, process the
            # current line to see what type of primitive is in play.
            elif building_draw:

                values = [numberize(v) for v in values]

                # Gather arcs.
                if line[0] == "A":
                    self.draw.append(DrawArc(*values))

                # Gather circles.
                elif line[0] == "C":
                    self.draw.append(DrawCircle(*values))

                # Gather polygons.
                elif line[0] == "P":
                    n_points = values[0]
                    points = values[4 : 4 + (2 * n_points)]
                    values = values[0:4] + [points]
                    if len(line) > (5 + len(points)):
                        values += [line[-1]]
                    else:
                        values += [""]
                    self.draw.append(DrawPoly(*values))

                # Gather rectangles.
                elif line[0] == "S":
                    self.draw.append(DrawRect(*values))

                # Gather text.
                elif line[0] == "T":
                    self.draw.append(DrawText(*values))

                # Gather the pin symbols. This is what we really want since
                # this defines the names, numbers and attributes of the
                # pins associated with the part.
                elif line[0] == "X":
                    # Get the information for this pin.
                    values[0:2] = line[
                        1:3
                    ]  # Restore pin num & name in case they were made into integers.
                    pin = DrawPin(*values)
                    try:
                        # See if the pin number already exists for this part.
                        rpt_pin = pins[pin.num]
                    except KeyError:
                        # No, this pin number is unique (so far), so store it
                        # using the pin number as the dict key.
                        self.draw.append(pin)
                        pins[pin.num] = pin
                    else:
                        # Uh, oh: Repeated pin number! Check to see if the
                        # duplicated pins have the same I/O type and unit num.
                        if (
                            pin.electrical_type != rpt_pin.electrical_type
                            or pin.unit != rpt_pin.unit
                        ):
                            active_logger.warning(
                                "Non-identical pins with the same number ({}) in symbol drawing {}".format(
                                    pin.num, self.name
                                )
                            )

                # Found something unknown in the drawing section.
                else:
                    msg = "Found something strange in {} symbol drawing: {}.".format(
                        self.name, line
                    )
                    active_logger.warning(msg)

            # Found something unknown outside the footprint list or drawing section.
            else:
                msg = "Found something strange in {} symbol definition: {}.".format(
                    self.name, line
                )
                active_logger.warning(msg)

    # Define some shortcuts to part information.
    self.num_units = int(self.definition["unit_count"])  # # of units within the part.
    self.name = self.definition["name"]  # Part name (e.g., 'LM324').
    self.ref_prefix = self.definition["reference"]  # Part ref prefix (e.g., 'R').

    # Clear the part reference field directly. Don't use the setter function
    # since it will try to generate and assign a unique part reference if
    # passed a value of None.
    self._ref = None

    # Make a Pin object from the information in the KiCad pin data fields.
    def kicad_pin_to_pin(kicad_pin):
        p = Pin()  # Create a blank pin.

        # Place the KiCad pin name, number and function fields to the Pin object.
        p.num = kicad_pin.num
        p.name = kicad_pin.name
        p.x = kicad_pin.x
        p.y = kicad_pin.y
        p.orientation = kicad_pin.orientation
        p.unit = kicad_pin.unit

        pin_type_translation = {
            "I": Pin.types.INPUT,
            "O": Pin.types.OUTPUT,
            "B": Pin.types.BIDIR,
            "T": Pin.types.TRISTATE,
            "P": Pin.types.PASSIVE,
            "U": Pin.types.UNSPEC,
            "W": Pin.types.PWRIN,
            "w": Pin.types.PWROUT,
            "C": Pin.types.OPENCOLL,
            "E": Pin.types.OPENEMIT,
            "N": Pin.types.NOCONNECT,
        }
        p.func = pin_type_translation[kicad_pin.electrical_type.upper()]

        return p

    self.pins = [kicad_pin_to_pin(p) for p in pins.values()]

    # Make sure all the pins have a valid reference to this part.
    self.associate_pins()

    # Create part units if there are more than 1.
    if self.num_units > 1:
        for i in range(1, self.num_units + 1):
            self.make_unit("u" + num_to_chars(i), unit=i)

    # Part definition has been parsed, so clear it out. This prevents a
    # part from being parsed more than once.
    self.part_defn = None


@export_to_all
def gen_pcb(circuit, pcb_file, fp_libs=None):
    """Create a KiCad PCB file directly from a Circuit object.

    Args:
        circuit (Circuit): Circuit object.
        pcb_file: Either a file object that can be written to, or a string
            containing a file name, or None.
        fp_libs: List of directories containing footprint libraries.
    Returns:
        None.
    """

    # Keep the import in here so it doesn't get triggered unless this is used
    # so it eases some problems with tox testing.
    # It requires pcbnew module which may not be present or may be for the
    # wrong Python version (2 vs. 3).
    try:
        import kinet2pcb  # For creating KiCad PCB directly from Circuit object.
    except ImportError:
        active_logger.warning(
            "kinet2pcb module is missing. Can't generate a KiCad PCB without it."
        )
    else:
        pcb_file = pcb_file or (get_script_name() + ".kicad_pcb")
        kinet2pcb.kinet2pcb(circuit, pcb_file, fp_libs)


@export_to_all
def gen_svg_comp(part, symtx, net_stubs=None):
    """
    Generate SVG for this component.

    Args:
        part: Part object for which an SVG symbol will be created.
        net_stubs: List of Net objects whose names will be connected to
            part symbol pins as connection stubs.
        symtx: String such as "HR" that indicates symbol mirroring/rotation.

    Returns: SVG for the part symbol."""

    def tx(obj, ops):
        """Transform Point, number, or direction according to the list of opcodes."""

        def H(obj):
            # Flip horizontally.
            if isinstance(obj, Point):
                return Point(-obj.x, obj.y)
            if isinstance(obj, (float, int)):
                return 180.0 - obj
            else:
                return {"U": "U", "D": "D", "L": "R", "R": "L"}[obj]

        def V(obj):
            # Flip vertically.
            if isinstance(obj, Point):
                return Point(obj.x, -obj.y)
            if isinstance(obj, (float, int)):
                return -obj
            else:
                return {"U": "D", "D": "U", "L": "L", "R": "R"}[obj]

        def R(obj):
            # Rotate right.
            if isinstance(obj, Point):
                return Point(-obj.y, obj.x)
            if isinstance(obj, (float, int)):
                return obj + 90.0
            else:
                return {"U": "R", "D": "L", "L": "U", "R": "D"}[obj]

        def L(obj):
            # Rotate left.
            if isinstance(obj, Point):
                return Point(obj.y, -obj.x)
            if isinstance(obj, (float, int)):
                return obj - 90.0
            else:
                return {"U": "L", "D": "R", "L": "D", "R": "U"}[obj]

        # Each character in ops applies a geometrical transformation.
        for op in ops:
            obj = locals()[op.upper()](obj)  # op selects the H, V, L, or R subroutine.
        return obj

    def draw_text(text, size, justify, origin, rotation, offset, class_, extra=""):
        return " ".join(
            [
                "<text",
                "class='{class_}'",
                "text-anchor='{justify}'",
                "x='{origin.x}' y='{origin.y}'",
                "transform='rotate({rotation} {origin.x} {origin.y}) translate({offset.x} {offset.y})'",
                "style='font-size:{size}px'",
                "{extra}",
                ">",
                "{text}",
                "</text>",
            ]
        ).format(**locals())

    def make_pin_dir_tbl(abs_xoff=20):

        # abs_xoff is the absolute distance of name/num from the end of the pin.
        rel_yoff_num = -0.15  # Relative distance of number above pin line.
        rel_yoff_name = (
            0.2  # Relative distance that places name midline even with pin line.
        )

        # Tuple for storing information about pins in each of four directions:
        #     direction: The direction the pin line is drawn from start to end.
        #     side: The side of the symbol the pin is on. (Opposite of the direction.)
        #     angle: The angle of the name/number text for the pin (usually 0, -90.).
        #     num_justify: Text justification of the pin number.
        #     name_justify: Text justification of the pin name.
        #     num_offset: (x,y) offset of the pin number w.r.t. the end of the pin.
        #     name_offset: (x,y) offset of the pin name w.r.t. the end of the pin.
        PinDir = namedtuple(
            "PinDir",
            "direction side angle num_justify name_justify num_offset name_offset net_offset",
        )

        return {
            "U": PinDir(
                Point(0, -1),
                "bottom",
                -90,
                "end",
                "start",
                Point(-abs_xoff, rel_yoff_num),
                Point(abs_xoff, rel_yoff_name),
                Point(abs_xoff, rel_yoff_num),
            ),
            "D": PinDir(
                Point(0, 1),
                "top",
                -90,
                "start",
                "end",
                Point(abs_xoff, rel_yoff_num),
                Point(-abs_xoff, rel_yoff_name),
                Point(-abs_xoff, rel_yoff_num),
            ),
            "L": PinDir(
                Point(-1, 0),
                "right",
                0,
                "start",
                "end",
                Point(abs_xoff, rel_yoff_num),
                Point(-abs_xoff, rel_yoff_name),
                Point(-abs_xoff, rel_yoff_num),
            ),
            "R": PinDir(
                Point(1, 0),
                "left",
                0,
                "end",
                "start",
                Point(-abs_xoff, rel_yoff_num),
                Point(abs_xoff, rel_yoff_name),
                Point(abs_xoff, rel_yoff_num),
            ),
        }

    fill_tbl = {"f": "background_fill", "F": "pen_fill", "N": ""}

    scale = 0.30  # Scale of KiCad units to SVG units.
    default_thickness = 1 / scale  # Default line thickness = 1.
    default_pin_name_offset = 20

    # Named tuple for storing component pin information.
    PinInfo = namedtuple("PinInfo", "x y side pid")

    # Get maximum length of net stub name if any are needed for this part symbol.
    net_stubs = net_stubs or []  # Empty list of stub nets if argument is None.
    max_stub_len = 0  # If no net stubs are needed, this stays at zero.
    for pin in part:
        for net in pin.nets:
            # Don't let names for no-connect nets affect maximum stub length.
            if net in [NC, None]:
                continue
            if net in net_stubs:
                max_stub_len = max(len(net.name), max_stub_len)

    # Go through each graphic object that makes up the component symbol.
    for obj in part.draw:

        obj_pin_info = (
            []
        )  # Component pin info so they can be generated once bbox is known.
        obj_svg = []  # Component graphic objects.
        obj_filled_svg = []  # Filled component graphic objects.
        obj_txt_svg = []  # Component text (because it has to be drawn last).
        obj_bbox = BBox()  # Bounding box of all the component objects.

        if isinstance(obj, DrawDef):
            def_ = obj
            show_name = def_.name[0] != "~"
            show_nums = def_.show_nums == "Y"
            show_names = def_.show_names == "Y"
            # Make pin direction table with symbol-specific name offset.
            pin_dir_tbl = make_pin_dir_tbl(def_.name_offset or default_pin_name_offset)
            # Make structures for holding info on each part unit.
            num_units = def_.num_units
            unit_pin_info = [[] for _ in range(num_units + 1)]
            unit_svg = [[] for _ in range(num_units + 1)]
            unit_filled_svg = [[] for _ in range(num_units + 1)]
            unit_txt_svg = [[] for _ in range(num_units + 1)]
            unit_bbox = [BBox() for _ in range(num_units + 1)]

        elif isinstance(obj, DrawF0):
            f0 = obj
            if f0.visibility != "I":
                # F0 field is not invisible.
                origin = tx(Point(f0.x, -f0.y), symtx) * scale
                orientation = f0.orientation + f0.halign
                dir = {
                    "HL": "L",
                    "HC": "L",
                    "HR": "R",
                    "VL": "D",
                    "VC": "D",
                    "VR": "U",
                }[orientation]
                dir = tx(dir, symtx)
                angle = pin_dir_tbl[dir].angle
                size = f0.size * scale
                justify = "middle" if f0.halign == "C" else pin_dir_tbl[dir].num_justify
                offset = (
                    tx(
                        {"T": Point(0, 1), "B": Point(0, 0), "C": Point(0, 0.5)}[
                            f0.valign[0]
                        ],
                        symtx,
                    )
                    * size
                )
                class_ = "part_ref_text"
                extra = 's:attribute="ref"'
                obj_txt_svg.append(
                    draw_text("X", size, justify, origin, angle, offset, class_, extra)
                )

        elif isinstance(obj, DrawF1):
            f1 = obj
            if f1.visibility != "I" and show_name:
                # F1 field is not invisible.
                origin = tx(Point(f1.x, -f1.y), symtx) * scale
                orientation = f1.orientation + f1.halign
                dir = {
                    "HL": "L",
                    "HC": "L",
                    "HR": "R",
                    "VL": "D",
                    "VC": "D",
                    "VR": "U",
                }[orientation]
                dir = tx(dir, symtx)
                angle = pin_dir_tbl[dir].angle
                size = f1.size * scale
                justify = "middle" if f1.halign == "C" else pin_dir_tbl[dir].num_justify
                offset = (
                    tx(
                        {"T": Point(0, 1), "B": Point(0, 0), "C": Point(0, 0.5)}[
                            f1.valign[0]
                        ],
                        symtx,
                    )
                    * size
                )
                class_ = "part_name_text"
                extra = 's:attribute="value"'
                obj_txt_svg.append(
                    draw_text("X", size, justify, origin, angle, offset, class_, extra)
                )

        elif isinstance(obj, DrawArc):
            arc = obj
            center = tx(Point(arc.cx, -arc.cy), symtx) * scale
            radius = arc.radius * scale
            start = tx(Point(arc.startx, -arc.starty), symtx) * scale
            end = tx(Point(arc.endx, -arc.endy), symtx) * scale
            start_angle = tx(arc.start_angle / 10, symtx)
            end_angle = tx(arc.end_angle / 10, symtx)
            clock_wise = int(end_angle < start_angle)
            large_arc = int(abs(end_angle - start_angle) > 180)
            thickness = (arc.thickness or default_thickness) * scale
            fill = fill_tbl.get(arc.fill, "")
            radius_pt = Point(radius, radius)
            obj_bbox.add(center - radius_pt)
            obj_bbox.add(center + radius_pt)
            svg = obj_filled_svg if fill else obj_svg
            svg.append(
                " ".join(
                    [
                        "<path",
                        'd="M {start.x} {start.y} A {radius} {radius} 0 {large_arc} {clock_wise} {end.x} {end.y}"',
                        'style="stroke-width:{thickness}"',
                        'class="$cell_id symbol {fill}"',
                        "/>",
                    ]
                ).format(**locals())
            )

        elif isinstance(obj, DrawCircle):
            circle = obj
            center = tx(Point(circle.cx, -circle.cy), symtx) * scale
            radius = circle.radius * scale
            thickness = (circle.thickness or default_thickness) * scale
            fill = fill_tbl.get(circle.fill, "")
            radius_pt = Point(radius, radius)
            obj_bbox.add(center - radius_pt)
            obj_bbox.add(center + radius_pt)
            svg = obj_filled_svg if fill else obj_svg
            svg.append(
                " ".join(
                    [
                        "<circle",
                        'cx="{center.x}" cy="{center.y}" r="{radius}"',
                        'style="stroke-width:{thickness}"',
                        'class="$cell_id symbol {fill}"',
                        "/>",
                    ]
                ).format(**locals())
            )

        elif isinstance(obj, DrawPoly):
            poly = obj
            pts = [
                tx(Point(x, -y), symtx) * scale
                for x, y in zip(poly.points[0::2], poly.points[1::2])
            ]
            path = []
            path_op = "M"
            for pt in pts:
                obj_bbox.add(pt)
                path.append("{path_op} {pt.x} {pt.y}".format(**locals()))
                path_op = "L"
            path = " ".join(path)
            thickness = (poly.thickness or default_thickness) * scale
            fill = fill_tbl.get(poly.fill, "")
            svg = obj_filled_svg if fill else obj_svg
            svg.append(
                " ".join(
                    [
                        "<path",
                        'd="{path}"',
                        'style="stroke-width:{thickness}"',
                        'class="$cell_id symbol {fill}"',
                        "/>",
                    ]
                ).format(**locals())
            )

        elif isinstance(obj, DrawRect):
            rect = obj
            start = tx(Point(rect.x1, -rect.y1), symtx) * scale
            end = tx(Point(rect.x2, -rect.y2), symtx) * scale
            obj_bbox.add(start)
            obj_bbox.add(end)
            rect_bbox = BBox(start, end)
            thickness = (rect.thickness or default_thickness) * scale
            fill = fill_tbl.get(rect.fill, "")
            svg = obj_filled_svg if fill else obj_svg
            svg.append(
                " ".join(
                    [
                        "<rect",
                        'x="{rect_bbox.min.x}" y="{rect_bbox.min.y}"',
                        'width="{rect_bbox.w}" height="{rect_bbox.h}"',
                        'style="stroke-width:{thickness}"',
                        'class="$cell_id symbol {fill}"',
                        "/>",
                    ]
                ).format(**locals())
            )

        elif isinstance(obj, DrawText):
            text = obj
            origin = tx(Point(text.x, -text.y), symtx) * scale
            angle = tx(text.angle, symtx)
            size = text.size * scale
            justify = {"L": "start", "C": "middle", "R": "end"}[text.halign]
            offset = (
                tx(
                    {"T": Point(0, 1), "B": Point(0, 0), "C": Point(0, 0.5)}[
                        text.valign
                    ],
                    symtx,
                )
                * size
            )
            obj_txt_svg.append(
                draw_text(
                    text.text, size, justify, origin, angle, offset, class_="part_text"
                )
            )

        elif isinstance(obj, DrawPin):

            pin = obj
            part_pin = part[
                pin.num
            ]  # Get Pin object associated with this pin drawing object.

            try:
                visible = pin.shape[0] != "N"
            except IndexError:
                visible = True  # No pin shape given, so it is visible by default.

            # Start pin group.
            orientation = tx(pin.orientation, symtx)
            dir = pin_dir_tbl[orientation].direction
            if part_pin.net in [None, NC]:
                # Unconnected pins remain at the length of the default symbol pin.
                extension = Point(0, 0)
            else:
                # Extend the pin if it's connected to a net.
                extension = (
                    dir
                    * (
                        pin.name_size * 0.5 * max_stub_len
                        + 2 * abs(pin_dir_tbl[orientation].net_offset.x)
                    )
                    * scale
                )
            start = tx(Point(pin.x, -pin.y), symtx) * scale - extension
            side = pin_dir_tbl[orientation].side
            obj_pin_info.append(PinInfo(x=start.x, y=start.y, side=side, pid=pin.num))

            if visible:
                # Draw pin if it's not invisible.

                # Create line for pin lead.
                l = dir * pin.length * scale
                end = start + l + extension
                thickness = default_thickness * scale
                obj_bbox.add(start)
                obj_bbox.add(end)
                obj_svg.append(
                    " ".join(
                        [
                            "<path",
                            'd="M {start.x} {start.y} L {end.x} {end.y}"',
                            'style="stroke-width:{thickness}"',
                            'class="$cell_id symbol"' "/>",
                        ]
                    ).format(**locals())
                )

                # Create pin number.
                if show_nums:
                    angle = pin_dir_tbl[orientation].angle
                    num_justify = pin_dir_tbl[orientation].num_justify
                    num_size = pin.num_size * scale
                    num_offset = pin_dir_tbl[orientation].num_offset * scale
                    num_offset.y = num_offset.y * pin.num_size
                    # Pin nums are text, but they go into graphical SVG because they are part of a pin object.
                    obj_svg.append(
                        draw_text(
                            str(pin.num),
                            num_size,
                            num_justify,
                            end,
                            angle,
                            num_offset,
                            "pin_num_text",
                        )
                    )

                # Create pin name.
                if pin.name != "~" and show_names:
                    name_justify = pin_dir_tbl[orientation].name_justify
                    name_size = pin.name_size * scale
                    name_offset = pin_dir_tbl[orientation].name_offset * scale
                    name_offset.y = name_offset.y * pin.name_size
                    # Pin names are text, but they go into graphical SVG because they are part of a pin object.
                    obj_svg.append(
                        draw_text(
                            str(pin.name),
                            name_size,
                            name_justify,
                            end,
                            angle,
                            name_offset,
                            "pin_name_text",
                        )
                    )

                # Create net stub name.
                if max_stub_len:
                    # Only do this if stub length > 0; otherwise, no stubs are needed.
                    for net in part_pin.nets:
                        # Don't create stubs for no-connect nets.
                        if net in [NC, None]:
                            continue
                        if net in net_stubs:
                            net_justify = pin_dir_tbl[orientation].name_justify
                            net_size = (
                                pin.name_size * scale
                            )  # Net name font size same as pin name font size.
                            net_offset = pin_dir_tbl[orientation].net_offset * scale
                            net_offset.y = net_offset.y * pin.name_size
                            obj_svg.append(
                                draw_text(
                                    net.name,
                                    net_size,
                                    net_justify,
                                    start,
                                    angle,
                                    net_offset,
                                    "net_name_text",
                                )
                            )
                            break  # Only one label is needed per stub.

        else:
            active_logger.error(
                "Unknown graphical object {} in part symbol {}.".format(
                    type(obj), part.name
                )
            )

        # Enter the current object into the SVG for this part.
        unit = getattr(obj, "unit", 0)
        if unit == 0:
            # Anything in unit #0 gets added to all units.
            for pin_info in unit_pin_info:
                pin_info.extend(obj_pin_info)
            for svg in unit_svg:
                svg.extend(obj_svg)
            for svg in unit_filled_svg:
                svg.extend(obj_filled_svg)
            for txt_svg in unit_txt_svg:
                txt_svg.extend(obj_txt_svg)
            for bbox in unit_bbox:
                bbox.add(obj_bbox)
        else:
            unit_pin_info[unit].extend(obj_pin_info)
            unit_svg[unit].extend(obj_svg)
            unit_filled_svg[unit].extend(obj_filled_svg)
            unit_txt_svg[unit].extend(obj_txt_svg)
            unit_bbox[unit].add(obj_bbox)

    # End of loop through all the component objects.

    # Assemble and name the SVGs for all the part units.
    svg = []
    for unit in range(1, num_units + 1):
        bbox = unit_bbox[unit]

        # Assign part unit name.
        if max_stub_len:
            # If net stubs are attached to symbol, then it's only to be used
            # for a specific part. Therefore, tag the symbol name with the unique
            # part reference so it will only be used by this part.
            symbol_name = "{part.name}_{part.ref}_{unit}_{symtx}".format(**locals())
        else:
            # No net stubs means this symbol can be used for any part that
            # also has no net stubs, so don't tag it with a specific part reference.
            symbol_name = "{part.name}_{unit}_{symtx}".format(**locals())

        # Begin SVG for part unit. Translate it so the bbox.min is at (0,0).
        translate = bbox.min * -1
        svg.append(
            " ".join(
                [
                    "<g",
                    's:type="{symbol_name}"',
                    's:width="{bbox.w}"',
                    's:height="{bbox.h}"',
                    'transform="translate({translate.x},{translate.y})"',
                    ">",
                ]
            ).format(**locals())
        )

        # Add part alias.
        svg.append('<s:alias val="{symbol_name}"/>'.format(**locals()))

        # Add part unit text and graphics.
        svg.extend(unit_filled_svg[unit])  # Filled items go on the bottom.
        svg.extend(unit_svg[unit])  # Then unfilled items.
        svg.extend(unit_txt_svg[unit])  # Text comes last.

        # Place a visible bounding-box around symbol for trouble-shooting.
        show_bbox = False
        if show_bbox:
            svg.append(
                " ".join(
                    [
                        "<rect",
                        'x="{bbox.min.x}" y="{bbox.min.y}"',
                        'width="{bbox.w}" height="{bbox.h}"',
                        'style="stroke-width:3; stroke:#f00"',
                        'class="$cell_id symbol"',
                        "/>",
                    ]
                ).format(**locals())
            )

        # Keep the pins out of the grouped text & graphics but adjust their coords
        # to account for moving the bbox.
        for pin_info in unit_pin_info[unit]:
            pin_pt = Point(pin_info.x, pin_info.y)
            side = pin_info.side
            pid = pin_info.pid
            pin_svg = '<g s:x="{pin_pt.x}" s:y="{pin_pt.y}" s:pid="{pid}" s:position="{side}"/>'.format(
                **locals()
            )
            svg.append(pin_svg)

        # Finish SVG for part unit.
        svg.append("</g>")

    return "\n".join(svg)
