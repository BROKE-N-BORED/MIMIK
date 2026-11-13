"""One shared flow fetch (item 32b).

Live proxy first, saved captures if the proxy is down. The CLI and
the GUI both used to carry their own copy of this — same capture
filter, same save, same fallback — and the copies had already
drifted in their log labels. Now there is one implementation; the
CLI adds its HAR/cURL branches and its empty-store error on top,
and the GUI adds its cache on top.
"""
from . import filters, log, store
from .sources import mitm as mitm_mod


def fetch_flows(where="fetch_flows"):
    """(mitm_or_None, flows, source) — source is "live" or "saved".

    Live flows pass the capture filter (item 26), then save to the
    store — a failed save is logged, never fatal. The proxy being
    down is not an error, it just means the saved path. `where`
    names the caller in the save-error log line.
    """
    try:
        m = mitm_mod.Mitm()
        flows = m.flows()
        # Capture filter (item 26): excluded hosts never reach the
        # store or the host list.
        flows, _dropped = filters.apply_capture_filter(flows)
        try:
            store.save_flows(flows, mitm=m)
        except Exception as e:
            log.log_error(f"{where} save_flows", e)
        return m, flows, "live"
    except mitm_mod.MitmError:
        return None, store.load_all(), "saved"
