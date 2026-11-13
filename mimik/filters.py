"""Capture filtering — exclude noisy hosts from a capture (item 26).

Telemetry, analytics, and crash-reporting hosts pollute the host list
and the store without ever being something the user wants a client
for. The exclusion list lives in settings.json (presets.py owns the
file), initialized to the conservative built-in defaults on first use
and user-editable; a separate toggle switches filtering on/off.

Matching is exact-host or subdomain: excluding `google-analytics.com`
also excludes `ssl.google-analytics.com`, but NOT
`evilgoogle-analytics.com` (the dot boundary matters).

This module is pure matching — it takes the list as a parameter and
never reads settings itself, except for apply_capture_filter's lazy
settings lookup (kept lazy so presets.py can import DEFAULT_EXCLUDED
without a cycle).
"""

# Conservative built-ins: hosts that are telemetry/analytics/crash
# reporting/ads only — never an app's own API. CDNs are deliberately
# NOT here (they serve real assets and sometimes APIs); the user can
# add them from the GUI or `mimik exclude add`.
DEFAULT_EXCLUDED = (
    "google-analytics.com",
    "googletagmanager.com",
    "app-measurement.com",
    "crashlytics.com",
    "sentry.io",
    "bugsnag.com",
    "mixpanel.com",
    "segment.io",
    "amplitude.com",
    "appcenter.ms",
    "doubleclick.net",
    "googlesyndication.com",
)


def _normalize(host):
    """Lowercase, stripped, no trailing dot."""
    return (host or "").strip().lower().rstrip(".")


def is_excluded(host, excluded):
    """True when host is on the list or is a subdomain of a listed
    host."""
    host = _normalize(host)
    if not host:
        return False
    for pattern in excluded or []:
        pattern = _normalize(pattern)
        if not pattern:
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def filter_flows(flows, excluded):
    """(kept, dropped_hosts) — flows whose request host is excluded
    are dropped; dropped_hosts names each excluded host seen."""
    kept = []
    dropped = set()
    for f in flows or []:
        req = f.get("request") or {}
        host = req.get("host") or ""
        if is_excluded(host, excluded):
            dropped.add(_normalize(host))
            continue
        kept.append(f)
    return kept, dropped


def apply_capture_filter(flows, base=None):
    """The capture filter for live flows: drops excluded hosts when
    filtering is on. Returns (kept, dropped_hosts). A no-op when
    filtering is off, so callers apply it unconditionally."""
    from . import presets

    if not presets.filtering_enabled(base):
        return flows, set()
    return filter_flows(flows, presets.excluded_hosts(base))
