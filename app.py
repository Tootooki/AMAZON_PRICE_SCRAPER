"""
Amazon ASIN Price Scraper — Web Application
Flask app with background processing, live progress, and Excel download.
"""

import os
import uuid
import threading
import io
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from scraper import run_scrape_job

app = Flask(__name__)

# In-memory job store (fine for single-worker deployment)
jobs = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def start_scrape():
    """Start a scraping job in the background."""
    data = request.get_json()
    raw_input = data.get("asins", "")

    # Parse ASINs: support newlines, commas, spaces, tabs
    asins = []
    for token in raw_input.replace(",", " ").replace("\t", " ").replace("\n", " ").split():
        token = token.strip().upper()
        if token and len(token) >= 5:  # Basic ASIN validation
            asins.append(token)

    # Deduplicate while preserving order
    seen = set()
    unique_asins = []
    for a in asins:
        if a not in seen:
            seen.add(a)
            unique_asins.append(a)

    if not unique_asins:
        return jsonify({"error": "No valid ASINs found. Please enter at least one ASIN."}), 400

    # Create job
    job_id = str(uuid.uuid4())[:8]
    job_state = {
        "status": "queued",
        "progress": 0,
        "total": len(unique_asins),
        "results": [],
        "error": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    jobs[job_id] = job_state

    # Start background thread
    thread = threading.Thread(
        target=run_scrape_job,
        args=(unique_asins, job_state),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id,
        "total_asins": len(unique_asins),
        "message": f"Scraping {len(unique_asins)} ASINs...",
    })


@app.route("/api/status/<job_id>")
def job_status(job_id):
    """Get the current status of a scraping job."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "results": job["results"],
        "error": job.get("error"),
    })


@app.route("/api/download/<job_id>")
def download_excel(job_id):
    """Download results as an Excel file."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] != "complete":
        return jsonify({"error": "Job not yet complete"}), 400

    # Build Excel workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Amazon Prices"

    # Styles
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill(start_color="1a1a2e", end_color="1a1a2e", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )

    ok_fill = PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid")
    err_fill = PatternFill(start_color="FFEBEE", end_color="FFEBEE", fill_type="solid")

    # Headers
    headers = ["#", "ASIN", "Price", "Title", "Status"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = thin_border

    # Data rows
    for i, result in enumerate(job["results"], 1):
        row = i + 1
        ws.cell(row=row, column=1, value=i).border = thin_border
        ws.cell(row=row, column=2, value=result["asin"]).border = thin_border
        ws.cell(row=row, column=3, value=result["price"]).border = thin_border
        ws.cell(row=row, column=4, value=result["title"]).border = thin_border
        ws.cell(row=row, column=5, value=result.get("error") or "OK").border = thin_border

        fill = ok_fill if result["status"] == "ok" else err_fill
        for col in range(1, 6):
            ws.cell(row=row, column=col).fill = fill

    # Column widths
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 60
    ws.column_dimensions["E"].width = 20

    # Freeze header row
    ws.freeze_panes = "A2"

    # Summary row
    summary_row = len(job["results"]) + 3
    ok_count = sum(1 for r in job["results"] if r["status"] == "ok")
    ws.cell(row=summary_row, column=1, value="Summary:").font = Font(bold=True)
    ws.cell(row=summary_row, column=2, value=f"{ok_count}/{len(job['results'])} prices found")

    # Save to bytes
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"amazon_prices_{timestamp}.xlsx"

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
