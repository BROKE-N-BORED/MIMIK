"""Shared path helpers — endpoint-shape generalization.

`generalize_path` is used by the code generator, the preset learner, and
the fingerprint matcher, so it lives here instead of in any one of them:
one definition of "what counts as an id in a URL path", shared by all.
"""
import re

_ID_PATTERNS = (
    # UUID
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I),
    # long hex blob
    re.compile(r"^[0-9a-f]{16,}$", re.I),
    # prefixed id: user-9f2c4e77a1b8, msg-001234
    re.compile(r"^[a-z]+-[0-9a-f]{8,}$", re.I),
    re.compile(r"^[a-z]+-\d{4,}$", re.I),
)


def _looks_like_id(seg):
    """True when a path segment is a concrete id, not a resource name."""
    if seg.isdigit():
        return True
    return any(p.match(seg) for p in _ID_PATTERNS)


def generalize_path(path):
    """A captured path with concrete ids replaced by {id}.

    /api/users/123 and /api/users/456 both become /api/users/{id};
    /posts/<uuid> becomes /posts/{id}. Literal segments stay, and
    segments already braced ({user_id}) are left alone. The generator
    uses it to collapse repeat endpoints into one method; presets
    learn endpoint shapes with it; fingerprints match on it.
    """
    path = (path or "").split("?", 1)[0]
    out = []
    for seg in [s for s in path.split("/") if s]:
        if re.fullmatch(r"\{\w+\}", seg):
            out.append(seg)
        elif _looks_like_id(seg):
            out.append("{id}")
        else:
            out.append(seg)
    return "/" + "/".join(out)
