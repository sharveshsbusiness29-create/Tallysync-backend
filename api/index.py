"""
Tally <-> Website backend, deployed on Vercel.

Live at: https://tallysync-backend.vercel.app

Storage: Vercel KV (Upstash Redis) - requires KV_REST_API_URL and
KV_REST_API_TOKEN environment variables, set automatically when a
KV/Upstash store is attached to this Vercel project.
"""

from flask import Flask, request, jsonify, render_template_string, Response
import json
import os
import uuid
import requests as http_requests
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime

app = Flask(__name__)

# --- Vercel KV storage (via Upstash REST API) ---
# Vercel provides these env vars automatically once you attach a KV
# store to your project (Storage tab -> Create Database -> KV).
KV_URL = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")


def kv_get(key, default):
    if not KV_URL:
        return default
    r = http_requests.get(f"{KV_URL}/get/{key}", headers={"Authorization": f"Bearer {KV_TOKEN}"})
    if r.status_code != 200:
        return default
    result = r.json().get("result")
    if result is None:
        return default
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return default
    # Defend against old double-encoded data from a previous bug:
    # if the first decode still yields a string, try decoding once more.
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, TypeError):
            return default
    if not isinstance(parsed, (list, dict)):
        return default
    return parsed


def kv_set(key, value):
    if not KV_URL:
        return
    try:
        r = http_requests.post(
            f"{KV_URL}/set/{key}",
            headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
            data=json.dumps(value),
            timeout=8,
        )
        if r.status_code != 200:
            # Record the failure so we can actually see it, since this
            # was previously being silently ignored no matter what
            # Upstash returned.
            try:
                http_requests.post(
                    f"{KV_URL}/set/debug_last_kv_set_error",
                    headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
                    data=json.dumps(f"key={key} status={r.status_code} body={r.text[:300]}"),
                    timeout=8,
                )
            except Exception:
                pass
    except Exception as e:
        try:
            http_requests.post(
                f"{KV_URL}/set/debug_last_kv_set_error",
                headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
                data=json.dumps(f"key={key} exception={str(e)[:300]}"),
                timeout=8,
            )
        except Exception:
            pass


@app.route("/debug/last-kv-error")
def debug_last_kv_error():
    err = kv_get_string("debug_last_kv_set_error", "(no kv_set error recorded)")
    return Response(f"<pre>{xml_escape(err)}</pre>", mimetype="text/html")


def load_invoices():
    return kv_get("invoices", [])


def save_invoices(invoices):
    kv_set("invoices", invoices)


def load_pending_ledgers():
    return kv_get("pending_ledgers", [])


def save_pending_ledgers(pending):
    kv_set("pending_ledgers", pending)


def load_tally_groups():
    return kv_get("tally_groups", [])


def save_tally_groups(groups):
    kv_set("tally_groups", groups)


def resolve_group_and_subgroup(immediate_parent, groups_list):
    """
    Walks up the group hierarchy from a ledger's immediate parent
    group to find the top-level Primary Group (Group) and the
    Sub-Group directly beneath it, matching the original spec:
    Group, Sub-Group, Ledger Name.

    Tally represents the true root of the hierarchy with a special
    internal marker (literally "Primary", sometimes with a leading
    control character) rather than a blank/empty parent - so that
    value must be treated as a stop condition, not chased as if it
    were a real group to look up.

    groups_list: [{"name": "...", "parent": "..."}, ...]
    """
    def is_root_marker(value):
        cleaned = value.strip().lstrip("\x00\x01\x02\x03\x04\x05").strip()
        return cleaned == "" or cleaned.lower() == "primary"

    group_lookup = {g["name"]: g.get("parent", "") for g in groups_list}

    chain = [immediate_parent]
    current = immediate_parent
    seen = set()
    while (
        current in group_lookup
        and not is_root_marker(group_lookup[current])
        and current not in seen
    ):
        seen.add(current)
        current = group_lookup[current]
        chain.append(current)

    top_group = chain[-1] if chain else immediate_parent
    sub_group = chain[-2] if len(chain) >= 2 else ""
    return top_group, sub_group


def load_tally_ledgers():
    return kv_get("tally_ledgers", [])


def save_tally_ledgers(ledgers):
    # Full replace every sync - matches "only one Chart of Accounts
    # active at a time" requirement. Old data is completely
    # discarded, not merged, each time new data arrives.
    kv_set("tally_ledgers", ledgers)


def load_tally_vouchers():
    return kv_get("tally_vouchers", [])


def save_tally_vouchers(vouchers):
    kv_set("tally_vouchers", vouchers)


# ---------- Website (simple approval UI) ----------

PAGE = """
<!DOCTYPE html>
<html>
<head>
<title>Tally Sync POC</title>
<style>
body { font-family: sans-serif; max-width: 700px; margin: 40px auto; }
table { width: 100%; border-collapse: collapse; }
th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
.pending { color: #b8860b; }
.posted { color: #2e7d32; }
form { margin-top: 30px; border-top: 1px solid #ccc; padding-top: 20px; }
input, select { padding: 6px; margin: 4px 0; width: 100%; box-sizing: border-box; }
button { margin-top: 10px; padding: 8px 16px; }
</style>
</head>
<body>
<h2>Tally Sync POC</h2>
<p>Add a test invoice below. TallyPrime's TDL script will pick up anything
with status "pending" on its next timer cycle and create it as a voucher.</p>

<table>
<tr><th>Invoice ID</th><th>Party Ledger</th><th>Expense Ledger</th><th>Amount</th><th>Status</th></tr>
{% for inv in invoices %}
<tr>
<td>{{ inv.invoice_id }}</td>
<td>{{ inv.party_ledger }}</td>
<td>{{ inv.expense_ledger }}</td>
<td>{{ inv.amount }}</td>
<td class="{{ inv.status }}">{{ inv.status }}</td>
</tr>
{% endfor %}
</table>

<h3>Chart of Accounts — P2P Setup</h3>
<p style="font-size: 0.9em; color: #666;">Ledgers matched by exact name so invoice approval produces a postable entry. Every ledger received from Tally is listed below, regardless of which fields are populated.</p>
<table>
<tr><th>Group</th><th>Sub-Group</th><th>Ledger Name</th><th>Mailing Name</th><th>Opening Balance</th><th>Closing Balance</th><th>Address</th><th>State</th><th>Pincode</th><th>Country</th><th>GSTIN/UIN</th><th>Branch</th><th>Bank Name</th><th>SWIFT Code</th><th>IFS Code</th><th>A/c No.</th><th>A/c Holder's Name</th><th>Phone</th><th>Mobile</th><th>Contact</th><th>Email</th></tr>
{% for l in tally_ledgers %}
<tr>
<td>{{ l.group }}</td>
<td>{{ l.sub_group }}</td>
<td>{{ l.name }}</td>
<td>{{ l.mailing_name }}</td>
<td>{{ l.opening_balance }}</td>
<td>{{ l.closing_balance }}</td>
<td>{{ l.address }}</td>
<td>{{ l.state }}</td>
<td>{{ l.pincode }}</td>
<td>{{ l.country }}</td>
<td>{{ l.gstin }}</td>
<td>{{ l.branch }}</td>
<td>{{ l.bank_name }}</td>
<td>{{ l.swift_code }}</td>
<td>{{ l.ifsc }}</td>
<td>{{ l.ac_number }}</td>
<td>{{ l.ac_holder_name }}</td>
<td>{{ l.phone }}</td>
<td>{{ l.mobile }}</td>
<td>{{ l.contact_person }}</td>
<td>{{ l.email }}</td>
</tr>
{% endfor %}
</table>

<h3>Create New Ledger (will sync to Tally)</h3>
<form method="POST" action="/add-ledger">
  <label>Ledger Name</label>
  <input name="name" placeholder="e.g. New Supplier Ltd" required>
  <label>Group (must match an exact Tally group name)</label>
  <input name="parent" placeholder="e.g. Sundry Creditors" required>
  <label>Opening Balance</label>
  <input name="opening_balance" type="number" value="0">
  <button type="submit">Add Ledger</button>
</form>

<table>
<tr><th>Request ID</th><th>Name</th><th>Group</th><th>Status</th></tr>
{% for p in pending_ledgers %}
<tr>
<td>{{ p.request_id }}</td>
<td>{{ p.name }}</td>
<td>{{ p.parent }}</td>
<td class="{{ 'posted' if p.status == 'created' else 'pending' }}">{{ p.status }}</td>
</tr>
{% endfor %}
</table>

<h3>Vouchers (mirrored from Tally)</h3>
<table>
<tr><th>Voucher Type</th><th>Ledger</th><th>Amount</th></tr>
{% for v in tally_vouchers %}
<tr>
<td>{{ v.voucher_type }}</td>
<td>{{ v.party_ledger }}</td>
<td>{{ v.amount }}</td>
</tr>
{% endfor %}
</table>

<form method="POST" action="/add-invoice">
  <label>Party Ledger (must match an exact Tally ledger name)</label>
  <input name="party_ledger" value="Akshaya Enterprises" required>
  <label>Expense Ledger (must match an exact Tally ledger name)</label>
  <input name="expense_ledger" value="Purchase" required>
  <label>Amount</label>
  <input name="amount" type="number" value="1000" required>
  <label>Narration</label>
  <input name="narration" value="Test invoice from POC website">
  <button type="submit">Add Pending Invoice</button>
</form>

</body>
</html>
"""


@app.route("/debug/ledgers")
def debug_ledgers():
    """
    Raw, unfiltered view of everything received for each ledger -
    for verifying the TDL pull is fetching correct real data before
    trimming the display down to the final P2P spec.
    """
    raw_ledgers = load_tally_ledgers()
    rows = ""
    all_keys = set()
    for l in raw_ledgers:
        all_keys.update(l.keys())
    all_keys = sorted(all_keys)

    header = "".join(f"<th>{k}</th>" for k in all_keys)
    for l in raw_ledgers:
        cells = "".join(f"<td>{l.get(k, '')}</td>" for k in all_keys)
        rows += f"<tr>{cells}</tr>"

    html = f"""
    <html><head><title>Debug: Raw Ledger Data</title>
    <style>table {{ border-collapse: collapse; }} td, th {{ border: 1px solid #ccc; padding: 6px; font-size: 0.85em; }}</style>
    </head><body>
    <h2>Raw Ledger Data (every field received, unfiltered)</h2>
    <p>{{count}} ledgers received. Columns shown are whatever fields TDL actually sent - compare against Tally directly.</p>
    <table><tr>{header}</tr>{rows}</table>
    </body></html>
    """.replace("{count}", str(len(raw_ledgers)))
    return html


def build_enriched_ledgers():
    raw_ledgers = load_tally_ledgers()
    tally_groups = load_tally_groups()
    enriched = []
    for l in raw_ledgers:
        top_group, sub_group = resolve_group_and_subgroup(l.get("parent", ""), tally_groups)
        enriched.append({
            "name": l.get("name", ""),
            "alias": l.get("alias", ""),
            "group": top_group,
            "sub_group": sub_group,
            "opening_balance": l.get("opening_balance", ""),
            "closing_balance": l.get("closing_balance", ""),
            "address": l.get("address", ""),
            "state": l.get("state", ""),
            "pincode": l.get("pincode", ""),
            "country": l.get("country", ""),
            "gstin": l.get("gstin", ""),
            "branch": l.get("branch", ""),
            "bank_name": l.get("bank_name", ""),
            "swift_code": l.get("swift_code", ""),
            "ifsc": l.get("ifsc", ""),
            "ac_number": l.get("ac_number", ""),
            "ac_holder_name": l.get("ac_holder_name", ""),
            "mailing_name": l.get("mailing_name", ""),
            "phone": l.get("phone", ""),
            "contact_person": l.get("contact_person", ""),
            "mobile": l.get("mobile", ""),
            "email": l.get("email", ""),
        })
    return enriched


LEDGER_DETAIL_PAGE = """
<!DOCTYPE html>
<html>
<head>
<title>{{ l.name }} - Ledger Detail</title>
<style>
body { font-family: sans-serif; max-width: 700px; margin: 40px auto; }
table { width: 100%; border-collapse: collapse; }
td, th { border: 1px solid #ccc; padding: 8px; text-align: left; }
th { width: 30%; background: #f5f5f5; }
a { color: #06c; }
</style>
</head>
<body>
<p><a href="/">&larr; Back to Chart of Accounts</a></p>
<h2>{{ l.name }}</h2>
<table>
<tr><th>Group</th><td>{{ l.group }}</td></tr>
<tr><th>Sub-Group</th><td>{{ l.sub_group }}</td></tr>
<tr><th>Mailing Name</th><td>{{ l.mailing_name }}</td></tr>
<tr><th>Opening Balance</th><td>{{ l.opening_balance }}</td></tr>
<tr><th>Closing Balance</th><td>{{ l.closing_balance }}</td></tr>
<tr><th>Address</th><td>{{ l.address }}</td></tr>
<tr><th>State</th><td>{{ l.state }}</td></tr>
<tr><th>Pincode</th><td>{{ l.pincode }}</td></tr>
<tr><th>Country</th><td>{{ l.country }}</td></tr>
<tr><th>GSTIN/UIN</th><td>{{ l.gstin }}</td></tr>
<tr><th>Branch</th><td>{{ l.branch }}</td></tr>
<tr><th>Bank Name</th><td>{{ l.bank_name }}</td></tr>
<tr><th>SWIFT Code</th><td>{{ l.swift_code }}</td></tr>
<tr><th>IFS Code</th><td>{{ l.ifsc }}</td></tr>
<tr><th>A/c No.</th><td>{{ l.ac_number }}</td></tr>
<tr><th>A/c Holder's Name</th><td>{{ l.ac_holder_name }}</td></tr>
<tr><th>Phone</th><td>{{ l.phone }}</td></tr>
<tr><th>Mobile</th><td>{{ l.mobile }}</td></tr>
<tr><th>Contact</th><td>{{ l.contact_person }}</td></tr>
<tr><th>Email</th><td>{{ l.email }}</td></tr>
</table>
</body>
</html>
"""


@app.route("/ledger/<name>")
def ledger_detail(name):
    ledgers = build_enriched_ledgers()
    match = next((l for l in ledgers if l["name"] == name), None)
    if not match:
        return f"<p>No ledger found matching '{name}'. <a href='/'>Back</a></p>", 404
    return render_template_string(LEDGER_DETAIL_PAGE, l=match)


@app.route("/")
def home():
    invoices = load_invoices()
    tally_vouchers = load_tally_vouchers()
    tally_ledgers = build_enriched_ledgers()
    return render_template_string(PAGE, invoices=invoices, tally_vouchers=tally_vouchers, tally_ledgers=tally_ledgers, pending_ledgers=load_pending_ledgers())


@app.route("/add-ledger", methods=["POST"])
def add_ledger():
    pending = load_pending_ledgers()
    new_ledger = {
        "request_id": "LDG-" + str(uuid.uuid4())[:8].upper(),
        "name": request.form["name"],
        "parent": request.form["parent"],
        "opening_balance": request.form.get("opening_balance", "0"),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }
    pending.append(new_ledger)
    save_pending_ledgers(pending)
    return home()


@app.route("/api/pending-ledgers", methods=["GET"])
@app.route("/api/pending-ledgers/<cache_bust>", methods=["GET"])
def pending_ledgers(cache_bust=None):
    """
    TDL Collection target for the website -> Tally direction.
    Accepts an optional path segment (e.g. /api/pending-ledgers/12345)
    that changes every request, to force a genuinely different URL
    each time in case Tally's HTTP client caches by exact URL
    regardless of cache-control headers.
    """
    pending = load_pending_ledgers()
    still_pending = [p for p in pending if p["status"] == "pending"]
    response = jsonify({"ledgers": still_pending})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/api/confirm-ledgers", methods=["POST"])
def confirm_ledgers_batch():
    """
    Batch confirmation, reusing the proven Repeat-based POST pattern
    (same mechanism that successfully confirmed invoices) instead of
    the single-object GET confirm, which never produced a walkable
    response no matter how it was structured.
    """
    data = request.get_json(force=True, silent=True) or {}
    envelope = data.get("ENVELOPE", data)
    entries = envelope.get("Ledger", [])
    if isinstance(entries, dict):
        entries = [entries]
    ids = [e.get("request_id") for e in entries if e.get("request_id")]

    pending = load_pending_ledgers()
    confirmed = 0
    for p in pending:
        if p["request_id"] in ids:
            p["status"] = "created"
            confirmed += 1
    save_pending_ledgers(pending)
    return jsonify({"status": "1", "confirmed_count": confirmed})


@app.route("/api/confirm-ledger/<request_id>", methods=["GET"])
def confirm_ledger(request_id):
    """
    TDL calls this after successfully creating the ledger in Tally.
    Returns an array-wrapped response (not a flat object) because
    Walk Collection needs something iterable to actually process -
    a flat single object silently produces zero walked rows.
    """
    pending = load_pending_ledgers()
    found = False
    for p in pending:
        if p["request_id"] == request_id:
            p["status"] = "created"
            found = True
            break
    save_pending_ledgers(pending)
    return jsonify({"results": [{"status": "1" if found else "0", "request_id": request_id}]})


@app.route("/add-invoice", methods=["POST"])
def add_invoice():
    invoices = load_invoices()
    new_invoice = {
        "invoice_id": "INV-" + str(uuid.uuid4())[:8].upper(),
        "date": "20260801",  # Educational Mode only accepts 1st/2nd/31st
        "party_ledger": request.form["party_ledger"],
        "expense_ledger": request.form["expense_ledger"],
        "amount": float(request.form["amount"]),
        "narration": request.form.get("narration", ""),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }
    invoices.append(new_invoice)
    save_invoices(invoices)
    return home()


# ---------- API endpoints for TDL ----------

@app.route("/api/pending-invoices", methods=["GET"])
def pending_invoices():
    """
    TDL Collection target. Matches the pattern already proven working:
    [Collection: PendingInvoicesColl]
    Data Source     : HTTP JSON : "http://localhost:5000/api/pending-invoices"
    JSON Object Path: "invoices:1"
    """
    invoices = load_invoices()
    pending = [inv for inv in invoices if inv["status"] == "pending"]
    return jsonify({"invoices": pending})


@app.route("/api/tally-sync", methods=["POST"])
def tally_sync():
    """
    Batch confirmation endpoint. TDL's Repeat-based export naturally
    sends ALL pending invoices in one POST as an array under "Sale",
    e.g. {"Sale": [{"invoice_id": "..."}, {"invoice_id": "..."}]}
    We mark every invoice_id found as posted.
    """
    data = request.get_json(force=True, silent=True) or {}
    # Tally automatically wraps the payload in "ENVELOPE" - check there first,
    # but also allow a flat structure for flexibility/testing.
    envelope = data.get("ENVELOPE", data)
    sale_entries = envelope.get("Sale", [])
    if isinstance(sale_entries, dict):
        sale_entries = [sale_entries]  # single item comes back as dict, not list

    confirmed_ids = [entry.get("invoice_id") for entry in sale_entries if entry.get("invoice_id")]

    invoices = load_invoices()
    count = 0
    for inv in invoices:
        if inv["invoice_id"] in confirmed_ids:
            inv["status"] = "posted"
            inv["posted_at"] = datetime.utcnow().isoformat()
            count += 1
    save_invoices(invoices)
    return jsonify({"status": "1", "confirmed_count": count, "confirmed_ids": confirmed_ids})


@app.route("/api/tally-groups", methods=["POST"])
def receive_tally_groups():
    """
    Receives ALL Groups (name + immediate parent) from Tally, used
    to reconstruct the full Group -> Sub-Group hierarchy for display.
    """
    data = request.get_json(force=True, silent=True) or {}
    envelope = data.get("ENVELOPE", data)
    group_entries = envelope.get("Group", [])
    if isinstance(group_entries, dict):
        group_entries = [group_entries]

    save_tally_groups(group_entries)
    return jsonify({"status": "1", "received_count": len(group_entries)})


@app.route("/api/tally-ledgers", methods=["POST"])
def receive_tally_ledgers():
    """
    Receives Chart of Accounts data (ledgers) read directly from
    Tally's own database (Type: Ledger collection), sent via the
    same Repeat-based POST pattern proven for vouchers/confirmation.
    """
    data = request.get_json(force=True, silent=True) or {}
    envelope = data.get("ENVELOPE", data)
    ledger_entries = envelope.get("Ledger", [])
    if isinstance(ledger_entries, dict):
        ledger_entries = [ledger_entries]

    save_tally_ledgers(ledger_entries)
    return jsonify({"status": "1", "received_count": len(ledger_entries)})


@app.route("/api/tally-ledgers", methods=["GET"])
def get_tally_ledgers():
    return jsonify({"ledgers": load_tally_ledgers()})


@app.route("/api/tally-vouchers", methods=["POST"])
def receive_tally_vouchers():
    """
    Receives vouchers TDL reads directly from Tally's own database
    (Type: Voucher collection), sent via the same Repeat-based POST
    pattern used for invoice confirmation. Payload arrives wrapped
    in ENVELOPE, same as the confirm endpoint.
    """
    data = request.get_json(force=True, silent=True) or {}
    envelope = data.get("ENVELOPE", data)
    voucher_entries = envelope.get("Voucher", [])
    if isinstance(voucher_entries, dict):
        voucher_entries = [voucher_entries]

    save_tally_vouchers(voucher_entries)
    return jsonify({"status": "1", "received_count": len(voucher_entries)})


@app.route("/api/tally-vouchers", methods=["GET"])
def get_tally_vouchers():
    return jsonify({"vouchers": load_tally_vouchers()})


@app.route("/api/confirm/<invoice_id>", methods=["GET"])
def confirm_invoice(invoice_id):
    """
    GET-based confirmation. TDL's proven HTTP JSON GET pattern
    (Data Source: HTTP JSON) can call this directly with the
    invoice ID embedded in the URL - no POST body needed at all.
    """
    invoices = load_invoices()
    found = False
    for inv in invoices:
        if inv["invoice_id"] == invoice_id:
            inv["status"] = "posted"
            inv["posted_at"] = datetime.utcnow().isoformat()
            found = True
            break
    save_invoices(invoices)
    return jsonify({"status": "1" if found else "0", "invoice_id": invoice_id})


# ---------- ERP 9 / XML compatible endpoints ----------
# These mirror the JSON endpoints above exactly, but speak XML,
# using the ENVELOPE structure confirmed in Tally's own ERP9 FAQ
# (Remote URL / Remote Request / XML Object Path pattern).

import re

_XML_ILLEGAL_CHARS_RE = re.compile(
    "[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]"
)


def sanitize_xml(raw_bytes_or_str):
    """
    Strips characters that are illegal inside XML 1.0 content.
    Tally emits raw control characters in some fields (notably the
    hidden marker byte, \\x04, used ahead of "Primary" for top-level
    groups) without escaping them, which produces technically
    invalid XML that a standards-compliant parser correctly rejects.
    """
    if isinstance(raw_bytes_or_str, bytes):
        text = raw_bytes_or_str.decode("utf-8", errors="replace")
    else:
        text = raw_bytes_or_str
    return _XML_ILLEGAL_CHARS_RE.sub("", text)


def parse_xml_body():
    """Parses an incoming XML request body wrapped in ENVELOPE,
    matching what TDL's Remote Request/XMLTAG structure sends."""
    cleaned = sanitize_xml(request.data)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as e:
        kv_set("debug_last_ledgers_xml_parse_error", f"{e} (body length: {len(request.data)} bytes)")
        return []
    # root is expected to be <ENVELOPE> containing repeated child tags
    return [{child.tag.lower(): (child.text or "") for child in item}
            for item in root]


@app.route("/debug/last-ledgers-parse-error")
def debug_last_ledgers_parse_error():
    err = kv_get_string("debug_last_ledgers_xml_parse_error", "(no parse error recorded)")
    return Response(f"<pre>{xml_escape(err)}</pre>", mimetype="text/html")


@app.route("/api/tally-ledgers-xml", methods=["POST"])
def receive_tally_ledgers_xml():
    raw = request.data.decode("utf-8", errors="replace")
    kv_set("debug_last_ledgers_xml_raw", raw)
    entries = parse_xml_body()
    kv_set("debug_last_ledgers_xml_parsed", entries)
    save_tally_ledgers(entries)
    return Response(
        f"<ENVELOPE><STATUS>1</STATUS><RECEIVED>{len(entries)}</RECEIVED></ENVELOPE>",
        mimetype="text/xml",
    )


@app.route("/debug/last-ledgers-parsed")
def debug_last_ledgers_parsed():
    parsed = kv_get("debug_last_ledgers_xml_parsed", [])
    return jsonify({"parsed_count": len(parsed), "parsed_entries": parsed})


def kv_get_string(key, default):
    """Like kv_get, but for plain string values (e.g. debug logs),
    not restricted to list/dict like the main kv_get is."""
    if not KV_URL:
        return default
    r = http_requests.get(f"{KV_URL}/get/{key}", headers={"Authorization": f"Bearer {KV_TOKEN}"})
    if r.status_code != 200:
        return default
    result = r.json().get("result")
    if result is None:
        return default
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return default
    return parsed if isinstance(parsed, str) else default


@app.route("/debug/version")
def debug_version():
    return "VERSION-CHECK-MARKER-002-sanitize-xml-fix-present"


@app.route("/debug/last-ledgers-xml")
def debug_last_ledgers_xml():
    raw = kv_get_string("debug_last_ledgers_xml_raw", "(nothing received yet)")
    return Response(f"<pre>{xml_escape(raw)}</pre>", mimetype="text/html")


@app.route("/api/tally-groups-xml", methods=["POST"])
def receive_tally_groups_xml():
    entries = parse_xml_body()
    save_tally_groups(entries)
    return Response(
        f"<ENVELOPE><STATUS>1</STATUS><RECEIVED>{len(entries)}</RECEIVED></ENVELOPE>",
        mimetype="text/xml",
    )


@app.route("/api/pending-ledgers-xml", methods=["GET"])
def pending_ledgers_xml():
    pending = load_pending_ledgers()
    still_pending = [p for p in pending if p["status"] == "pending"]
    rows = ""
    for p in still_pending:
        rows += (
            "<LEDGER>"
            f"<REQUESTID>{xml_escape(p.get('request_id',''))}</REQUESTID>"
            f"<REQUESTNAME>{xml_escape(p.get('name',''))}</REQUESTNAME>"
            f"<REQUESTPARENT>{xml_escape(p.get('parent',''))}</REQUESTPARENT>"
            f"<REQUESTOPENINGBALANCE>{xml_escape(str(p.get('opening_balance','0')))}</REQUESTOPENINGBALANCE>"
            "</LEDGER>"
        )
    # Matches the confirmed structure from Tally's own ERP9 FAQ example:
    # TALLYMESSAGE:1:ENVELOPE:BODY:1
    xml_body = (
        "<TALLYMESSAGE>"
        "<ENVELOPE>"
        f"<BODY>{rows}</BODY>"
        "</ENVELOPE>"
        "</TALLYMESSAGE>"
    )
    response = Response(xml_body, mimetype="text/xml")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.route("/api/confirm-ledgers-xml", methods=["POST"])
def confirm_ledgers_xml():
    entries = parse_xml_body()
    ids = [e.get("request_id") for e in entries if e.get("request_id")]
    pending = load_pending_ledgers()
    confirmed = 0
    for p in pending:
        if p["request_id"] in ids:
            p["status"] = "created"
            confirmed += 1
    save_pending_ledgers(pending)
    return Response(
        f"<ENVELOPE><STATUS>1</STATUS><CONFIRMED>{confirmed}</CONFIRMED></ENVELOPE>",
        mimetype="text/xml",
    )


# Vercel imports `app` directly from this file as the WSGI handler
