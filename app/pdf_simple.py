"""A small PDF writer (no dependency): A4 pages with Helvetica text, lines and filled boxes, enough for the printed reports
sent to clients (Mémoire de règlement). Text is written in WinAnsi (cp1252), which holds the French accents."""
import unicodedata

PAGE_W, PAGE_H = 595.28, 841.89

# Helvetica advance widths (1/1000 em) of the ASCII characters 32..126 (Adobe AFM).
_WIDTHS = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]


def text_width(value: str, size: float, bold: bool = False) -> float:
    total = 0
    for char in value:
        base = unicodedata.normalize("NFKD", char)[:1] or char
        code = ord(base)
        total += _WIDTHS[code - 32] if 32 <= code <= 126 else 556
    return total * size / 1000 * (1.06 if bold else 1)


def fit(value: str, width: float, size: float, bold: bool = False) -> str:
    """The text cut (with "…") to fit the width."""
    value = str(value or "")
    if text_width(value, size, bold) <= width:
        return value
    while value and text_width(value + "…", size, bold) > width:
        value = value[:-1]
    return value + "…"


def _escape(value: str) -> bytes:
    raw = str(value).encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Pdf:
    """Coordinates from the top left corner, in points (y grows downwards)."""

    def __init__(self) -> None:
        self.pages: list[list[bytes]] = []
        self.new_page()

    def new_page(self) -> None:
        self.pages.append([])

    @property
    def _ops(self) -> list[bytes]:
        return self.pages[-1]

    def text(self, x: float, y: float, value: str, size: float = 9, bold: bool = False, align: str = "left",
             color: tuple[float, float, float] = (0.14, 0.24, 0.29)) -> None:
        if align != "left":
            width = text_width(value, size, bold)
            x -= width if align == "right" else width / 2
        font = b"/F2" if bold else b"/F1"
        r, g, b = color
        self._ops.append(b"BT %s %.2f Tf %.3f %.3f %.3f rg %.2f %.2f Td (" % (font, size, r, g, b, x, PAGE_H - y) + _escape(value) + b") Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5, gray: float = 0.75) -> None:
        self._ops.append(b"%.3f G %.2f w %.2f %.2f m %.2f %.2f l S" % (gray, width, x1, PAGE_H - y1, x2, PAGE_H - y2))

    def box(self, x: float, y: float, w: float, h: float, fill: tuple[float, float, float] | None = None,
            stroke: float | None = None) -> None:
        ops = b""
        if fill:
            ops += b"%.3f %.3f %.3f rg " % fill
        if stroke is not None:
            ops += b"%.3f G 0.6 w " % stroke
        ops += b"%.2f %.2f %.2f %.2f re " % (x, PAGE_H - y - h, w, h)
        ops += b"B" if fill and stroke is not None else b"f" if fill else b"S"
        self._ops.append(ops)

    def output(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")  # filled below
        pages_id = add(b"")
        font1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        font2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        for ops in self.pages:
            stream = b"\n".join(ops)
            content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            kids.append(add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_id, PAGE_W, PAGE_H, font1, font2, content)))
        objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
        objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
        return bytes(out)
