"""
Calculate MAX_CHARS_PER_LINE and MAX_LINES_PER_CHUNK for text_cues.py
from the QLab stage resolution, the font, and the subtitle area on screen.
Optionally checks readability from the TV diagonal and viewing distance.

The font is measured with one of these backends (first available wins):
  1. pyobjc    - macOS text engine via PyObjC (exact, includes kerning)
  2. jxa       - macOS text engine via osascript JavaScript; built into
                 every Mac, no install needed (per-character widths)
  3. estimate  - rough built-in character-width table (no font access)

Examples:
    python3 calc_text_layout.py
    python3 calc_text_layout.py --font Tahoma --style Bold --size 72 \
        --stage 1920x1080 --diagonal 65 --distance 6
    python3 calc_text_layout.py --script Subtitles_Script_November_10th_EN.txt
    python3 calc_text_layout.py --backend jxa
"""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import textwrap

# Defaults come from text_cues.py so both scripts stay in sync
try:
    import text_cues
    DEFAULT_FONT = text_cues.FONT_FAMILY or "Helvetica"
    DEFAULT_STYLE = text_cues.FONT_STYLE
    DEFAULT_SIZE = text_cues.FONT_SIZE
except Exception:
    DEFAULT_FONT, DEFAULT_STYLE, DEFAULT_SIZE = "Helvetica", "Regular", 72

# Representative English text, used when no script file is given
SAMPLE_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Welcome to tonight's performance, we hope you enjoy the show! "
    "Where were you when the lights went out? I was waiting, as always, "
    "for something wonderful to happen in this old theatre."
)

# Rule of thumb: capital letters should be at least 1/200 of the viewing distance
MIN_CAP_HEIGHT_RATIO = 1 / 200


class FontError(Exception):
    """Font family or style not found."""


# -----------------------------
# Backend 1: PyObjC (exact)
# -----------------------------
class PyObjCMetrics:
    name = "pyobjc (exact)"

    def __init__(self, family, style, size):
        import AppKit  # raises ImportError if PyObjC is missing
        self.AppKit = AppKit

        members = AppKit.NSFontManager.sharedFontManager() \
            .availableMembersOfFontFamily_(family)
        if not members:
            raise FontError(f'Font family "{family}" not found. Check the name in Font Book.')
        styles = {str(m[1]): str(m[0]) for m in members}  # style -> PostScript name
        if style not in styles:
            raise FontError(f'Style "{style}" not found for "{family}". '
                            f'Available: {", ".join(styles)}')

        self.font = AppKit.NSFont.fontWithName_size_(styles[style], size)
        self.attrs = {AppKit.NSFontAttributeName: self.font}
        self.line_height = AppKit.NSLayoutManager.alloc().init() \
            .defaultLineHeightForFont_(self.font)
        self.cap_height = self.font.capHeight()

    def width(self, text):
        s = self.AppKit.NSAttributedString.alloc() \
            .initWithString_attributes_(text, self.attrs)
        return s.size().width


# -----------------------------
# Backend 2: JXA via osascript (no install needed)
# -----------------------------
JXA_SCRIPT = r"""
ObjC.import('AppKit');
function run(argv) {
    const [family, style, size, chars] = argv;
    const members = ObjC.deepUnwrap(
        $.NSFontManager.sharedFontManager.availableMembersOfFontFamily(family));
    if (!members) return JSON.stringify({error: 'family'});
    const member = members.find(m => m[1] === style);
    if (!member) return JSON.stringify({error: 'style', styles: members.map(m => m[1])});

    const font = $.NSFont.fontWithNameSize(member[0], Number(size));
    const attrs = $.NSDictionary.dictionaryWithObjectForKey(font, $.NSFontAttributeName);
    const widths = {};
    for (const c of Array.from(chars)) {
        widths[c] = $.NSString.stringWithString(c).sizeWithAttributes(attrs).width;
    }
    return JSON.stringify({
        widths: widths,
        lineHeight: $.NSLayoutManager.alloc.init.defaultLineHeightForFont(font),
        capHeight: font.capHeight
    });
}
"""


class JXAMetrics:
    name = "jxa (per-character widths, no kerning)"

    def __init__(self, family, style, size, text):
        if not shutil.which("osascript"):
            raise RuntimeError("osascript not available")

        chars = "".join(sorted(set(text) | {" "}))
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", JXA_SCRIPT,
             family, style, str(size), chars],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

        data = json.loads(result.stdout)
        if data.get("error") == "family":
            raise FontError(f'Font family "{family}" not found. Check the name in Font Book.')
        if data.get("error") == "style":
            raise FontError(f'Style "{style}" not found for "{family}". '
                            f'Available: {", ".join(data["styles"])}')

        self.widths = data["widths"]
        self.line_height = data["lineHeight"]
        self.cap_height = data["capHeight"]

    def width(self, text):
        # Summing single-character widths ignores kerning, which usually makes
        # lines slightly narrower, so this errs on the safe side.
        return sum(self.widths.get(c, 0) for c in text)


# -----------------------------
# Backend 3: rough estimate (no font access)
# -----------------------------
class EstimateMetrics:
    name = "estimate (rough, font not measured)"

    # Approximate character widths as a fraction of the font size,
    # based on typical sans-serif fonts such as Helvetica / Arial / Tahoma.
    CLASSES = [
        ("ijl.,:;'!|", 0.25),
        ("ftrI()[]\"- ", 0.33),
        ("mwMW", 0.85),
        ("ABCDEGHKNOQRUVXY&", 0.70),
        ("FJLPSTZ0123456789", 0.58),
    ]
    DEFAULT_WIDTH = 0.53       # other lowercase letters and symbols
    BOLD_FACTOR = 1.08

    def __init__(self, family, style, size):
        bold = "bold" in style.lower() or "black" in style.lower()
        scale = size * (self.BOLD_FACTOR if bold else 1.0)
        self.table = {}
        for chars, w in self.CLASSES:
            for c in chars:
                self.table[c] = w * scale
        self.default = self.DEFAULT_WIDTH * scale
        self.line_height = size * 1.2
        self.cap_height = size * 0.72

    def width(self, text):
        return sum(self.table.get(c, self.default) for c in text)


def get_metrics(backend, family, style, size, text):
    """Pick the first backend that works, or the one requested."""
    order = ["pyobjc", "jxa", "estimate"] if backend == "auto" else [backend]
    for b in order:
        try:
            if b == "pyobjc":
                return PyObjCMetrics(family, style, size)
            if b == "jxa":
                return JXAMetrics(family, style, size, text)
            return EstimateMetrics(family, style, size)
        except FontError as e:
            sys.exit(str(e))
        except Exception as e:
            print(f"[{b} unavailable: {e}]", file=sys.stderr)
    sys.exit("No measuring backend available.")


def read_script_text(path):
    """Read the script, dropping speaker prefixes like 'PROFESSOR:'."""
    speaker = re.compile(r'^[A-Z0-9 ]+:\s*')
    with open(path, encoding="utf-8") as f:
        return " ".join(speaker.sub("", line.strip()) for line in f if line.strip())


def max_chars_that_fit(text, metrics, max_width_px):
    """
    Largest wrap width (in characters) where every line textwrap produces
    fits within max_width_px. This matches how text_cues.py wraps text.
    """
    best = 0
    for n in range(10, 200):
        lines = textwrap.wrap(text, width=n,
                              break_long_words=False, break_on_hyphens=False)
        if all(metrics.width(line) <= max_width_px for line in lines):
            best = n
        else:
            break
    return best


def parse_pair(value, sep):
    a, b = value.lower().split(sep)
    return float(a), float(b)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--font", default=DEFAULT_FONT, help="font family (Font Book name)")
    p.add_argument("--style", default=DEFAULT_STYLE, help="font style, e.g. Regular, Bold")
    p.add_argument("--size", type=float, default=DEFAULT_SIZE, help="font size in points")
    p.add_argument("--stage", default="1920x1080", help="QLab stage resolution, WxH")
    p.add_argument("--margin", type=float, default=0.05,
                   help="side margin on each side, fraction of width (default 0.05)")
    p.add_argument("--area", type=float, default=1 / 3,
                   help="fraction of screen height for subtitles (default 1/3)")
    p.add_argument("--px-per-pt", type=float, default=1.0,
                   help="stage pixels per font point (default 1.0, verify in QLab)")
    p.add_argument("--diagonal", type=float, help="TV diagonal in inches")
    p.add_argument("--aspect", default="16:9", help="TV aspect ratio (default 16:9)")
    p.add_argument("--distance", type=float,
                   help="viewing distance of the farthest viewer, in meters")
    p.add_argument("--script", help="script file to measure instead of sample text")
    p.add_argument("--backend", default="auto",
                   choices=["auto", "pyobjc", "jxa", "estimate"],
                   help="how to measure the font (default: auto)")
    args = p.parse_args()

    stage_w, stage_h = parse_pair(args.stage, "x")
    text = read_script_text(args.script) if args.script else SAMPLE_TEXT
    metrics = get_metrics(args.backend, args.font, args.style,
                          args.size * args.px_per_pt, text)

    usable_w = stage_w * (1 - 2 * args.margin)
    usable_h = stage_h * args.area
    max_chars = max_chars_that_fit(text, metrics, usable_w)
    max_lines = max(1, int(usable_h // metrics.line_height))

    print(f"Font:            {args.font} {args.style}, {args.size:g} pt")
    print(f"Measured with:   {metrics.name}")
    print(f"Stage:           {stage_w:g} x {stage_h:g} px")
    print(f"Usable width:    {usable_w:.0f} px  (margins {args.margin:.0%} each side)")
    print(f"Subtitle area:   {usable_h:.0f} px  ({args.area:.0%} of height)")
    print(f"Line height:     {metrics.line_height:.0f} px")
    print(f"Measured on:     {'script ' + args.script if args.script else 'sample text'}")
    print()
    print(f"MAX_CHARS_PER_LINE = {max_chars}")
    print(f"MAX_LINES_PER_CHUNK = {max_lines}")

    if args.diagonal:
        aw, ah = parse_pair(args.aspect, ":")
        screen_h_cm = args.diagonal * 2.54 * ah / math.hypot(aw, ah)
        cap_cm = metrics.cap_height * screen_h_cm / stage_h
        max_dist_m = cap_cm / MIN_CAP_HEIGHT_RATIO / 100

        print()
        print(f"Screen height:   {screen_h_cm:.1f} cm")
        print(f"Capital height:  {cap_cm:.1f} cm on screen")
        print(f"Readable up to:  ~{max_dist_m:.1f} m (rule of thumb: cap height >= distance/200)")

        if args.distance:
            if args.distance <= max_dist_m:
                print(f"OK: farthest viewer at {args.distance:g} m should read it comfortably.")
            else:
                needed = args.size * args.distance / max_dist_m
                print(f"TOO SMALL for {args.distance:g} m: use about {math.ceil(needed)} pt "
                      f"or larger.")


if __name__ == "__main__":
    main()
