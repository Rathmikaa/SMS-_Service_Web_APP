import csv
import io
import os
import time
import re
import uuid

from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, flash

import db
import parser as pdf_parser
import sms_gateway

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DEFAULT_TEMPLATE = (
    "GCC, Zone-13, Div-{dn} Property tax due for bill no {bill_number} sl no {sl_no} "
    "Name: {owner_name}, New Door No {new_door_no}, Street: {street}. "
    "Half Yearly Tax: {current_tax_due} Arrear: {arrear_due} Total Balance: Rs {balance_amount} "
    "Pay immediately via https://chennaicorporation.gov.in/gcc/online-payment/ or GCC Mobile App "
    "to avoid interest/penalty."
)

db.init_db()


def render_message(template, rec):
    try:
        return template.format(**rec)
    except (KeyError, IndexError) as e:
        return f"[template error: missing field {e}]"


def _to_int(v):
    v = (v or "0").replace(",", "").strip()
    try:
        return int(float(v))
    except ValueError:
        return 0


def _record_from_form(form):
    """Build a record dict from the manual add/edit form. Used for both
    creating a brand-new test record and saving edits to an existing one."""
    bill_number = form.get("bill_number", "").strip() or f"MANUAL-{uuid.uuid4().hex[:8].upper()}"
    return {
        "bill_number": bill_number,
        "sl_no": form.get("sl_no", "").strip(),
        "dn": form.get("dn", "").strip(),
        "owner_name": form.get("owner_name", "").strip(),
        "new_door_no": form.get("new_door_no", "").strip(),
        "old_door_no": form.get("old_door_no", "").strip(),
        "street": form.get("street", "").strip(),
        "mobile": re.sub(r"\D", "", form.get("mobile", ""))[-10:],
        "property_type": form.get("property_type", "").strip(),
        "property_usage": form.get("property_usage", "").strip(),
        "current_tax_due": _to_int(form.get("current_tax_due")),
        "arrear_due": _to_int(form.get("arrear_due")),
        "balance_amount": _to_int(form.get("balance_amount")),
        "remarks": form.get("remarks", "").strip(),
        "source_file": "manual entry",
        "needs_review": False,
    }


@app.route("/")
def index():
    filter_mobile = request.args.get("mobile", "")
    status = request.args.get("status", "")
    search = request.args.get("q", "")
    review_only = request.args.get("review") == "1"
    records = db.list_records(
        filter_mobile=filter_mobile or None, status=status or None, search=search or None,
        review_only=review_only,
    )
    template = db.get_setting("message_template", DEFAULT_TEMPLATE)
    provider_name = os.environ.get("SMS_PROVIDER", "console")
    return render_template(
        "index.html",
        records=records,
        stats=db.stats(),
        template=template,
        provider_name=provider_name,
        filter_mobile=filter_mobile,
        status=status,
        search=search,
        review_only=review_only,
        sample_message=render_message(template, records[0]) if records else "",
    )


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("pdfs")
    if not files or all(f.filename == "" for f in files):
        flash("Choose at least one PDF file.", "error")
        return redirect(url_for("index"))

    total_inserted, total_updated, total_parsed = 0, 0, 0
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", f.filename)
        path = os.path.join(UPLOAD_DIR, f"{int(time.time())}_{safe_name}")
        f.save(path)
        try:
            records = pdf_parser.parse_pdf(path, source_filename=f.filename)
        except Exception as e:  # noqa: BLE001
            flash(f"Failed to parse {f.filename}: {e}", "error")
            continue
        inserted, updated = db.upsert_records(records)
        total_inserted += inserted
        total_updated += updated
        total_parsed += len(records)

    flash(
        f"Parsed {total_parsed} rows from {len(files)} file(s): "
        f"{total_inserted} new, {total_updated} updated (duplicates merged by bill number).",
        "success",
    )
    return redirect(url_for("index"))


@app.route("/manual-add", methods=["POST"])
def manual_add():
    record = _record_from_form(request.form)
    if not record["owner_name"] and not record["mobile"]:
        flash("Enter at least an owner name or a mobile number.", "error")
        return redirect(url_for("index"))
    db.upsert_records([record])
    flash(f"Test record saved (bill no {record['bill_number']}). Select it below to preview or send.", "success")
    return redirect(url_for("index"))


@app.route("/records/<int:record_id>/update", methods=["POST"])
def update_record(record_id):
    record = _record_from_form(request.form)
    try:
        db.update_record(record_id, record)
    except Exception as e:  # noqa: BLE001 - e.g. bill_number collision
        flash(f"Could not save changes: {e}", "error")
        return redirect(url_for("index"))
    flash("Record updated.", "success")
    return redirect(url_for("index"))


@app.route("/template", methods=["POST"])
def save_template():
    template = request.form.get("template", DEFAULT_TEMPLATE)
    db.set_setting("message_template", template)
    flash("Message template saved.", "success")
    return redirect(url_for("index"))


@app.route("/preview", methods=["POST"])
def preview():
    data = request.get_json(force=True)
    template = data.get("template", DEFAULT_TEMPLATE)
    ids = data.get("ids", [])
    records = db.get_records_by_ids(ids)
    previews = [
        {"id": r["id"], "owner_name": r["owner_name"], "mobile": r["mobile"], "message": render_message(template, r)}
        for r in records
    ]
    return jsonify({"previews": previews})


@app.route("/send", methods=["POST"])
def send():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    template = data.get("template", DEFAULT_TEMPLATE)
    dry_run = bool(data.get("dry_run", True))
    throttle_ms = int(data.get("throttle_ms", 300))

    db.set_setting("message_template", template)
    records = db.get_records_by_ids(ids)
    provider = sms_gateway.get_provider() if not dry_run else sms_gateway.ConsoleProvider()

    results = []
    for r in records:
        if not r["mobile"]:
            results.append({"id": r["id"], "owner_name": r["owner_name"], "skipped": True, "reason": "no mobile number"})
            continue
        message = render_message(template, r)
        success, response_text = provider.send(r["mobile"], message)
        db.log_send(r["id"], r["mobile"], message, success, dry_run, provider.name, response_text)
        db.update_status(r["id"], "sent" if success else "failed")
        results.append(
            {
                "id": r["id"],
                "owner_name": r["owner_name"],
                "mobile": r["mobile"],
                "success": success,
                "response": response_text,
            }
        )
        if throttle_ms and not dry_run:
            time.sleep(throttle_ms / 1000.0)

    sent_count = sum(1 for x in results if x.get("success"))
    failed_count = sum(1 for x in results if "success" in x and not x["success"])
    skipped_count = sum(1 for x in results if x.get("skipped"))
    return jsonify(
        {
            "results": results,
            "summary": {"sent": sent_count, "failed": failed_count, "skipped": skipped_count},
            "dry_run": dry_run,
            "provider": provider.name,
        }
    )


@app.route("/logs")
def logs():
    return render_template("logs.html", logs=db.list_logs())


@app.route("/export.csv")
def export_csv():
    records = db.list_records()
    buf = io.StringIO()
    if records:
        writer = csv.DictWriter(buf, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=gcc_tax_records.csv"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")