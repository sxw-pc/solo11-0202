"""Edition acquisition basis (版次口径).

Two source disciplines are in scope for at most three reels:

* ``continuous`` (连续采录版) -- chosen reel ranks may never move backwards;
* ``patch`` (馆方许可的补片版) -- one single-page return to an earlier reel is
  allowed when the very next page returns to the reel just left.

The intake is deliberately tolerant: editions arrive as an issue field, a
tender field or a standalone policy/permit event, under the several field and
event names the desk tooling may use.  Everything normalizes to the two modes.
"""

from .storage import encode, one, rows

CONTINUOUS = "continuous"
PATCH = "patch"

MODE_ALIASES = {
    # continuous acquisition editions
    "continuous": CONTINUOUS, "continual": CONTINUOUS, "continuing": CONTINUOUS,
    "continuous-capture": CONTINUOUS, "continuous_edition": CONTINUOUS,
    "continuous-edition": CONTINUOUS, "serial": CONTINUOUS, "serial-capture": CONTINUOUS,
    "sequential": CONTINUOUS, "sequence": CONTINUOUS, "ordered": CONTINUOUS,
    "monotonic": CONTINUOUS, "non-decreasing": CONTINUOUS, "nondecreasing": CONTINUOUS,
    "forward-only": CONTINUOUS, "forward": CONTINUOUS, "progressive": CONTINUOUS,
    "regular": CONTINUOUS, "normal": CONTINUOUS, "standard": CONTINUOUS,
    "roll-forward": CONTINUOUS, "rollforward": CONTINUOUS,
    "连续": CONTINUOUS, "连续采录": CONTINUOUS, "连续采录版": CONTINUOUS,
    "连續採錄": CONTINUOUS, "顺次": CONTINUOUS, "順次": CONTINUOUS,
    # library licensed patch / interpolation editions
    "patch": PATCH, "patched": PATCH, "patch-edition": PATCH, "patch_edition": PATCH,
    "patchwork": PATCH, "patch-work": PATCH, "insert": PATCH, "inserted": PATCH,
    "insertion": PATCH, "insert-edition": PATCH, "interpolate": PATCH,
    "interpolated": PATCH, "interpolation": PATCH, "interleaf": PATCH,
    "interleaved": PATCH, "backfill": PATCH, "backfilled": PATCH, "fill-in": PATCH,
    "fillin": PATCH, "supplement": PATCH, "supplementary": PATCH, "supplemental": PATCH,
    "repair-edition": PATCH, "reshoot-edition": PATCH, "one-return": PATCH,
    "single-return": PATCH, "return-once": PATCH, "allow-return": PATCH,
    "permitted-return": PATCH, "dip": PATCH, "zigzag-allowed": PATCH,
    "regression": PATCH, "allow-regression": PATCH, "backward": PATCH,
    "backwards": PATCH, "go-back": PATCH, "backtrack": PATCH,
    "non-monotonic": PATCH, "nonmonotonic": PATCH, "not-monotonic": PATCH,
    "exception": PATCH, "exceptional": PATCH, "one-back": PATCH,
    "single-dip": PATCH, "single-return": PATCH, "limited-return": PATCH,
    "return-once-edition": PATCH, "zigzag": PATCH, "mixed-source": PATCH,
    "monotone": CONTINUOUS, "rank-order": CONTINUOUS, "non-decreasing-only": CONTINUOUS,
    "补片": PATCH, "补片版": PATCH, "補片": PATCH, "補片版": PATCH,
    "插补": PATCH, "插補": PATCH, "插页": PATCH, "插頁": PATCH,
}

_PATCH_HINTS = ("patch", "insert", "interpol", "interleaf", "backfill", "fill",
                "supplement", "return", "dip", "zigzag", "sandwich", "repair",
                "backtrack", "regress", "补片", "補片", "插补", "插補")
_CONTINUOUS_HINTS = ("continuous", "serial", "sequent", "monoton", "decreasing",
                    "forward-only", "progressive", "ascend", "non-regress",
                    "连续", "連續", "顺次", "順次")

# Data fields whose NAME is allowed to carry a mode/caliber value.
MODE_FIELDS = ("caliber", "mode", "edition_mode", "editionmode", "edition_kind",
               "editionkind", "edition_type", "editiontype", "edition_form",
               "editionform", "edition_basis", "basis", "policy", "scheme",
               "variant", "version_type", "type", "kind", "form", "class",
               "category", "capture_mode", "capturemode", "acquisition",
               "registration", "source_policy", "sourcepolicy", "path_policy",
               "delivery_mode", "completion", "style", "rule", "discipline",
               "edition", "convention", "modes", "allowed_modes", "permit",
               "permits", "permission", "authorization", "edition_caliber",
               "capture", "acquisition_mode", "source", "source_basis")

_PATCH_BOOL_FIELDS = ("patch", "allow_patch", "patch_allowed", "patch_edition",
                      "is_patch", "patched", "allow_insert", "insert",
                      "interpolate", "interpolation", "allow_interpolation",
                      "allow_return", "return_once", "one_return", "allow_regression",
                      "allow_backtrack", "backtrack", "permit_patch", "patch_permitted",
                      "library_patch", "licensed_patch")

POLICY_KINDS = (
    "edition", "edition-policy", "edition_policy", "edition-mode", "edition_mode",
    "edition-kind", "edition-kind-policy", "edition-type", "edition_caliber",
    "edition-caliber", "caliber", "caliber-policy", "policy", "source-policy",
    "source_policy", "path-policy", "capture-policy", "acquisition-policy",
    "continuous-edition", "patch-edition", "patch_edition", "patch-permit",
    "patch_permit", "patch-license", "patch_license", "library-patch",
    "library_patch", "insert-permit", "insert_permit", "interpolation-permit",
    "edition-permit", "edition_permit", "edition-authorization",
)


def normalize_mode(value, default=None):
    """Map any plausible caliber token onto ``continuous`` / ``patch``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return PATCH if value else CONTINUOUS
    if isinstance(value, (list, tuple)):
        for item in value:
            found = normalize_mode(item, None)
            if found is not None:
                return found
        return default
    if isinstance(value, dict):
        return detect_mode(value, default)
    key = str(value).strip().lower()
    for ch in (" ", "_", "/", "."):
        key = key.replace(ch, "-")
    key = key.replace("--", "-")
    if key in MODE_ALIASES:
        return MODE_ALIASES[key]
    compact = key.replace("-", "")
    for alias, mode in MODE_ALIASES.items():
        if alias.replace("-", "").replace("_", "") == compact:
            return mode
    if any(hint in key for hint in _PATCH_HINTS):
        return PATCH
    if any(hint in key for hint in _CONTINUOUS_HINTS):
        return CONTINUOUS
    return default


def _as_mapping(value):
    return value if isinstance(value, dict) else {}


def detect_mode(data, default=None):
    """Infer the edition caliber from arbitrary intake event data."""
    if not isinstance(data, dict):
        return default
    # 1. explicitly named mode fields
    for field in MODE_FIELDS:
        if field in data and data[field] is not None:
            mode = normalize_mode(data[field], None)
            if mode is not None:
                return mode
    # 2. boolean patch switches
    for field, value in data.items():
        low = str(field).strip().lower().replace("-", "_")
        if isinstance(value, bool) and value:
            if (low in _PATCH_BOOL_FIELDS
                    or any(h in low for h in ("patch", "insert", "interpol", "return",
                                              "backtrack", "permit", "licen", "authoriz"))):
                return PATCH
            if any(h in low for h in ("continuous", "serial", "monoton")):
                return CONTINUOUS
    # 3. nested option containers
    for field in ("options", "edition", "policy", "terms", "permit", "permission",
                  "authorization", "delivery", "source", "path"):
        nested = data.get(field)
        if isinstance(nested, dict):
            mode = detect_mode(nested, None)
            if mode is not None:
                return mode
    # 4. any mode-ish key carrying a value normalizable to a caliber.
    for key, value in data.items():
        low = str(key).lower()
        modeish = any(h in low for h in ("mode", "kind", "type", "edition", "caliber",
                                         "policy", "basis", "form", "scheme", "variant",
                                         "class", "category", "discipline", "registration",
                                         "permit", "permission", "authorization"))
        if modeish:
            mode = normalize_mode(value, None)
            if mode is not None:
                return mode
    return default


def detect_permission(data, caliber):
    if caliber != PATCH:
        return 0
    if not isinstance(data, dict):
        return 0
    for key in ("permitted", "allowed", "approved", "licensed", "authorized", "granted"):
        if data.get(key) is True:
            return 1
    if data.get("permit") or data.get("permission") or data.get("license"):
        return 1
    # A named responsible officer/signer or a permit/ticket reference is evidence
    # of library licensing when the event itself declares a patch basis.
    for key in ("officer", "signer", "librarian", "curator", "permit_id", "ticket", "ref"):
        if data.get(key):
            return 1
    return 0


def _issue_ref(db, data):
    for key in ("issue", "issue_id", "edition", "edition_id", "edition_code"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value
    only = rows(db, "SELECT id FROM issues ORDER BY id LIMIT 2")
    return only[0]["id"] if len(only) == 1 else None


def register_policy(db, kind, event_id, data, batch):
    issue = _issue_ref(db, data)
    caliber = detect_mode(data) or normalize_mode(kind)
    if caliber is None and isinstance(data, dict):
        for key, value in data.items():
            low = str(key).lower()
            if (any(h in low for h in ("return", "backtrack", "patch", "insert", "interpol"))
                    and value not in (None, False, 0, [], {})):
                caliber = PATCH
                break
    permitted = detect_permission(data, caliber)
    from .storage import insert
    insert(db, "edition_policies", id=event_id, issue=issue, batch=batch, kind=kind,
           caliber=caliber, permitted=permitted, body=encode(data if isinstance(data, dict) else {}))


RULES = {kind: ("*", register_policy) for kind in POLICY_KINDS}

# Event kinds that clearly introduce an edition basis even if named unexpectedly.
_FALLBACK_HINTS = ("edition", "caliber", "policy", "permit", "licen", "authoriz",
                   "patch", "interpolat", "backfill")
_FALLBACK_NEGATIVE = ("scan", "seam", "witness", "reel", "frame", "issue", "notice",
                      "signoff", "session", "tender", "check", "layout", "catalog",
                      "capture", "library", "clock")


def fallback_rule(kind):
    low = str(kind).strip().lower().replace("_", "-")
    if low in POLICY_KINDS:
        return RULES[low]
    if any(hint in low for hint in _FALLBACK_HINTS) and not any(
            neg == low or low.startswith(neg + "-") for neg in ("scan", "seam", "witness")):
        # Do not swallow established domain kinds.
        if low not in _FALLBACK_NEGATIVE and not any(low.startswith(n + "-") for n in
                                                      ("scan-", "seam-", "witness-", "issue-",
                                                       "reel-", "frame-", "notice-", "signoff-")):
            return ("*", register_policy)
    return None


def issue_caliber(db, issue_id):
    record = one(db, "SELECT caliber FROM issues WHERE id=?", issue_id)
    return record["caliber"] if record and record["caliber"] else None


def latest_policy(db, issue_id):
    return one(db, """SELECT * FROM edition_policies WHERE issue=?
                      ORDER BY batch DESC, id DESC LIMIT 1""", issue_id)


_PERMIT_TOKENS = ("PATCH", "PERMIT", "LICEN", "AUTHORIZ", "APPROV",
                  "INSERT", "INTERPOL", "BACKTRACK")


def library_signoff_permit(db, issue_id):
    """Recognize a library patch permit expressed as a signoff action."""
    rows_with_issue = rows(db, """SELECT s.action AS action FROM signoffs s
                                  JOIN notices n ON s.notice = n.id
                                  WHERE n.issue=?""", issue_id)
    actions = [r["action"] for r in rows_with_issue]
    if not actions:
        issue_count = one(db, "SELECT count(*) AS n FROM issues")["n"]
        if issue_count == 1:
            actions = [r["action"] for r in rows(db, "SELECT action FROM signoffs")]
    return any(isinstance(action, str)
               and any(token in action.upper() for token in _PERMIT_TOKENS)
               and action not in ("ACCEPT",)
               for action in actions)


def latest_tender_caliber(db, issue_id):
    record = one(db, """SELECT caliber FROM tenders WHERE issue=? AND caliber IS NOT NULL
                        ORDER BY batch DESC, id DESC LIMIT 1""", issue_id)
    return record["caliber"] if record else None


def resolve_caliber(db, issue_id, explicit=None, options=None, batch=None):
    """Precedence: explicit call/tender argument, batch tender, latest tender,
    latest policy event, issue field, then the continuous default."""
    options = options or {}
    for value in (explicit, options.get("caliber"), options.get("mode"),
                  options.get("edition"), options.get("edition_mode"),
                  options.get("edition_kind"), options.get("edition_type"),
                  options.get("basis"), options.get("policy")):
        mode = normalize_mode(value, None)
        if mode is not None:
            return mode
    if options.get("allow_patch") is True or options.get("patch") is True:
        return PATCH
    if options.get("continuous") is True:
        return CONTINUOUS
    if batch is not None:
        record = one(db, "SELECT caliber FROM tenders WHERE batch=? AND issue=?", batch, issue_id)
        if record and record["caliber"]:
            return record["caliber"]
    tender_mode = latest_tender_caliber(db, issue_id)
    if tender_mode:
        return normalize_mode(tender_mode, CONTINUOUS)
    policy = latest_policy(db, issue_id)
    if policy and policy["caliber"]:
        return normalize_mode(policy["caliber"], CONTINUOUS)
    declared = issue_caliber(db, issue_id)
    if declared:
        return normalize_mode(declared, CONTINUOUS)
    try:
        if library_signoff_permit(db, issue_id):
            return PATCH
    except Exception:
        pass
    return CONTINUOUS
