"""ICS text helpers — escaping and line folding for DESCRIPTION values.

Shared by fetch_menu.py and fetch_breakfast.py the way notify.py already is.

WHY THIS EXISTS: the event notes went from a short fixed string to a sentence
plus a URL, which crosses two RFC 5545 rules the old one-liner never touched.

  ESCAPING — a literal ";" or "," inside a TEXT value is a field separator
  unless escaped. "Menus follow the school's published calendar; actual meals
  may vary" contains a semicolon, so an unescaped version would corrupt every
  event for strict parsers.

  FOLDING — a content line over 75 octets must be folded onto continuation
  lines beginning with a space. A URL plus a sentence is comfortably over.

Neither failure is loud: the file still looks like a calendar, and the damage
shows up as missing or mangled notes on somebody's phone.
"""


def escape(value: str) -> str:
    """Escape a value for an ICS TEXT property (RFC 5545 3.3.11).

    Backslash first, or it would escape the escapes added after it.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold one content line to 75 octets (RFC 5545 3.1), CRLF + space.

    Splits on octets rather than characters, and never mid-UTF-8-sequence or
    immediately after a backslash — breaking an escape pair across the fold is
    legal but trips lenient parsers in the wild.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    pieces, limit = [], 75
    while len(raw) > limit:
        cut = limit
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        while cut > 1 and raw[cut - 1:cut] == b"\\":
            cut -= 1
        pieces.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
        limit = 74  # continuation lines spend one octet on the leading space

    pieces.append(raw.decode("utf-8"))
    return "\r\n ".join(pieces)


def description(menu_url: str) -> str:
    """The DESCRIPTION line for a meal event, escaped and folded.

    The published-menu link is what a parent taps when the calendar and the
    cafeteria disagree — it settles whether the feed misread the menu or the
    school served something else. Items are deliberately NOT repeated here;
    they are already the event title.

    An empty menu_url omits the link line rather than publishing a broken one.
    A missing link is a smaller problem than a dead one in every event.
    """
    lines = []
    if menu_url:
        lines.append(f"Full published menu available at {menu_url}")
    lines.append("Menus follow the school's published calendar; actual meals "
                 "may vary. Confirm with your school.")
    return fold("DESCRIPTION:" + escape("\n\n".join(lines)))
