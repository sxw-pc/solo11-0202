"""Preview collected text strips with their originating scan and box references."""

from .capture import assess
from .storage import natural, one, require, text


def collect(db, packet):
    require(isinstance(packet, dict) and set(packet) == {"scan", "strips"}, "invalid transcript packet")
    scan_id, source = packet["scan"], packet["strips"]
    require(text(scan_id) and isinstance(source, list) and source, "invalid transcript source")
    scan = assess(db).get(scan_id)
    if not scan or scan["state"] != "USABLE":
        return {"state": "SOURCE_PENDING", "scan": scan_id, "strips": [], "text": ""}
    frame = one(db, "SELECT * FROM frames WHERE id=?", scan["frame"])
    strips = []
    seen = {}
    for row in source:
        require(isinstance(row, dict) and set(row) == {"id", "line", "left", "right", "text"}, "invalid strip")
        require(text(row["id"]) and text(row["text"]) and all(natural(row[k]) for k in ("line", "left", "right")),
                "invalid strip field")
        require(row["left"] < row["right"], "empty strip box")
        if row["id"] in seen:
            require(seen[row["id"]] == row, "conflicting strip identifier")
            continue
        seen[row["id"]] = row
        strips.append(row)
    ordered = sorted(strips, key=lambda row: (row["line"], row["left"], row["id"]))
    for left, right in zip(ordered, ordered[1:]):
        require(left["line"] != right["line"] or left["right"] <= right["left"], "overlapping strip boxes")
    pieces = []
    position = 0
    previous_line = None
    for strip in ordered:
        if pieces:
            position += 1
        separator = "" if not pieces else " " if previous_line == strip["line"] else "\n"
        value = strip["text"]
        pieces.append((separator, strip | {"start": position, "end": position + len(value)}))
        position += len(value)
        previous_line = strip["line"]
    return {"state": "READY", "scan": scan_id, "frame": scan["frame"], "image": scan["image"],
            "page": frame["page"], "text": "".join(sep + row["text"] for sep, row in pieces),
            "strips": [row for _, row in pieces]}
