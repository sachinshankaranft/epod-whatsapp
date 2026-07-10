"""
ePOD Guardian - WhatsApp bot (single file)
FreightTiger hackathon demo

WHAT IT DOES
  A driver sends a POD photo to your Twilio WhatsApp sandbox number.
  This app: receives it -> downloads the image -> reads it with OpenAI vision
  -> matches the waybill number to a known journey -> validates -> replies in
  the SAME WhatsApp chat with the verdict.

RUN LOCALLY (with ngrok, simplest)
  1. pip install fastapi uvicorn requests python-multipart
  2. export OPENAI_API_KEY=sk-...            # your OpenAI key (needs credit)
     export TWILIO_ACCOUNT_SID=AC...          # from Twilio console
     export TWILIO_AUTH_TOKEN=...             # from Twilio console (secret)
  3. python epod_whatsapp.py                  # starts on port 8010
  4. In another terminal:  ngrok http 8010
  5. Copy the ngrok https URL, go to Twilio console ->
     Messaging -> Try it out -> Send a WhatsApp message -> "Sandbox settings",
     set "When a message comes in" to:  <ngrok-url>/whatsapp   (POST)
  6. From your phone (already joined the sandbox) send a POD photo. Done.

  Also open  http://localhost:8010  for a web view of every submission.

NOTES
  - Keys are read from the environment, never stored in this file.
  - Matching is on the waybill number read off the image.
  - Update JOURNEYS below anytime to add deliveries.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime

import requests
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

# ============================================================================
# JOURNEY DATA  (source of truth incoming PODs are matched against)
# Built from the three Safexpress waybills provided. Materials are placeholders
# (not legible on the photos) - swap in real codes/rates anytime.
# ============================================================================
JOURNEYS = [
    {
        "journey_fteid": "JRN-JBP-8894",
        "waybill_number": "6907 0473 8894",
        "consignor_name": "Tata Motors Ltd",
        "transporter_name": "Safexpress",
        "consignee_name": "Frontier Trucks (Jabalpur)",
        "total_invoice_value": 36335,
        "materials": [{"material_code": "M001", "description": "Auto components"}],
        "rate_card": {"M001": 380},
    },
    {
        "journey_fteid": "JRN-RJP-0354",
        "waybill_number": "7007 0446 0354",
        "consignor_name": "TML Commercial Vehicles Limited",
        "transporter_name": "Safexpress",
        "consignee_name": "Libra Automotors (Patiala)",
        "total_invoice_value": 1041,
        "materials": [{"material_code": "M001", "description": "Auto components"}],
        "rate_card": {"M001": 380},
    },
    {
        "journey_fteid": "JRN-PTNA-2299",
        "waybill_number": "5007 0496 2299",
        "consignor_name": "Tata Motors Limited",
        "transporter_name": "Safexpress",
        "consignee_name": "Binay Motors Private Limited (Patna)",
        "total_invoice_value": 76000,
        "materials": [{"material_code": "M001", "description": "Auto components"}],
        "rate_card": {"M001": 380},
    },
]

CRITICAL = ["waybill", "lr", "invoice", "number", "material", "signature", "stamp", "damage"]

# in-memory record of everything processed (for the web view)
HISTORY: list[dict] = []


def norm(v):
    return "" if v is None else "".join(ch for ch in str(v).upper() if ch.isalnum())


def find_journey(waybill_number):
    n = norm(waybill_number)
    for j in JOURNEYS:
        if norm(j["waybill_number"]) == n:
            return j
    return None


# ============================================================================
# Validation engine
# ============================================================================
def run_engine(ocr, journey):
    checks, reasons = [], []

    def add(check, status, reason=None):
        checks.append({"check": check, "status": status, "reason": reason})

    illegible = ocr.get("illegible_fields") or []
    crit = [f for f in illegible if any(k in str(f).lower() for k in CRITICAL)]
    if crit:
        r = ("Document not readable: " + ", ".join(crit)
             + ". Please retake with the full document in frame and good lighting.")
        add("Legibility", "FAIL", r)
        return {"verdict": "REJECTED", "condition": None, "checks": checks,
                "reasons": [r], "shortage": None}
    add("Legibility", "PASS")

    if journey is None:
        r = ("Waybill number on this document doesn't match any open delivery. "
             "Please check you sent the correct POD.")
        add("Waybill match", "FAIL", r)
        return {"verdict": "REJECTED", "condition": None, "checks": checks,
                "reasons": [r], "shortage": None}
    add("Waybill match", "PASS")

    damage = bool(ocr.get("damage_or_shortage"))
    notes = ocr.get("damage_notes") or []
    if not damage:
        add("Condition (damage / shortage)", "PASS", "No damage or shortage found")
        return {"verdict": "AUTO_APPROVED", "condition": "CLEAN", "checks": checks,
                "reasons": [], "shortage": None}

    r = ("Damage/shortage recorded: " + "; ".join(notes) + "."
         if notes else "Damage/shortage indicated on the document.")
    add("Condition (damage / shortage)", "FAIL", r)
    return {"verdict": "PENDING_L1", "condition": "UNCLEAN", "checks": checks,
            "reasons": [r], "shortage": {"items": ocr.get("shortage_items") or [], "notes": notes}}


def eval_debit(shortage_items, rate_card, invoice_value):
    value, breakdown = 0.0, []
    for it in shortage_items or []:
        rate = rate_card.get(it.get("material_code"))
        qty = it.get("shortage_qty") or 0
        if rate and qty:
            value += qty * rate
            breakdown.append({**it, "rate": rate, "computed": qty * rate})
    if value <= 0:
        return {"raise": True, "amount": None, "breakdown": breakdown}
    return {"raise": True, "amount": value, "breakdown": breakdown}


# ============================================================================
# OpenAI vision
# ============================================================================
VISION_PROMPT = """You are validating a proof-of-delivery (POD) / Safexpress waybill. Return ONLY valid JSON with these keys:
{"waybill_number": string|null, "consignor": string|null, "consignee": string|null, "legible": boolean, "illegible_fields": string[], "damage_or_shortage": boolean, "damage_notes": string[], "shortage_items": [{"material_code": string, "shortage_qty": number}], "signature_present": boolean, "stamp_present": boolean}

LEGIBILITY - only CRITICAL fields matter: the waybill number, and the signature/stamp/damage region.
- Set legible=false and list a field ONLY IF a critical field cannot be read at all.
- Minor fields (DIM, DOD/DACC, package weight, freight amount, dates) do NOT affect legibility. Never list them.
- A normal phone photo with some blur is fine.
The waybill number is the long number near the top right (also in the barcode), format like "5007 0496 2299".
damage_or_shortage=true only if there's a handwritten/printed note of damage, breakage, shortage, or missing quantity. A normal signature+stamp with no such note = false. Use false/null when unsure."""


def extract_with_openai(image_bytes, media_type):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    b64 = base64.b64encode(image_bytes).decode()
    res = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "max_tokens": 1000,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                ],
            }],
        },
        timeout=60,
    )
    text = res.json()["choices"][0]["message"]["content"]
    return json.loads(text.replace("```json", "").replace("```", "").strip())


# ============================================================================
# Twilio media download + reply
# ============================================================================
def download_twilio_media(media_url):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    r = requests.get(media_url, auth=(sid, token), timeout=60)
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "image/jpeg")


def twiml_reply(message):
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return PlainTextResponse(
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>',
        media_type="application/xml",
    )


def verdict_message(result, journey, debit):
    if result["verdict"] == "AUTO_APPROVED":
        return (f"✅ POD ACCEPTED\n\n"
                f"Waybill: {journey['waybill_number']}\n"
                f"Consignee: {journey['consignee_name']}\n"
                f"Clean delivery — no damage found.\n"
                f"Billing is now unlocked. You're all set, thank you!")
    if result["verdict"] == "PENDING_L1":
        amt = (f"₹{debit['amount']:,.0f}" if debit and debit.get("amount") else "to be confirmed")
        return (f"⚠️ POD RECEIVED — UNCLEAN\n\n"
                f"Waybill: {journey['waybill_number']}\n"
                f"{result['reasons'][0]}\n"
                f"This will go for L1 approval. A debit note ({amt}) may be raised. "
                f"Thank you for reporting it.")
    # rejected
    return (f"❌ POD REJECTED\n\n{result['reasons'][0]}\n\n"
            f"Please retake and send again.")


# ============================================================================
# App
# ============================================================================
app = FastAPI()


@app.post("/whatsapp")
async def whatsapp(request: Request):
    form = await request.form()
    num_media = int(form.get("NumMedia", 0))
    sender = form.get("From", "")

    if num_media == 0:
        return twiml_reply(
            "👋 Send a photo of the signed POD / waybill and I'll validate it instantly.")

    media_url = form.get("MediaUrl0")
    media_type = form.get("MediaContentType0", "image/jpeg")
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "from": sender}
    try:
        image_bytes, ctype = download_twilio_media(media_url)
        ocr = extract_with_openai(image_bytes, ctype)
        journey = find_journey(ocr.get("waybill_number"))
        result = run_engine(ocr, journey)
        debit = None
        if result["verdict"] == "PENDING_L1":
            debit = eval_debit(result["shortage"]["items"],
                               journey["rate_card"] if journey else {},
                               journey["total_invoice_value"] if journey else None)
        msg = verdict_message(result, journey, debit) if journey or result["verdict"] != "REJECTED" \
            else verdict_message(result, {"waybill_number": ocr.get("waybill_number")}, None)
        entry.update({"waybill": ocr.get("waybill_number"), "verdict": result["verdict"],
                      "journey": journey["journey_fteid"] if journey else None})
        HISTORY.insert(0, entry)
        return twiml_reply(msg)
    except Exception as e:
        entry.update({"verdict": "ERROR", "error": str(e)})
        HISTORY.insert(0, entry)
        return twiml_reply("⚠️ Sorry, I couldn't read that image. Please retake it clearly and resend.")


@app.get("/", response_class=HTMLResponse)
def home():
    rows = "".join(
        f"<tr><td>{h['time']}</td><td>{h.get('waybill','-')}</td>"
        f"<td>{h.get('verdict','-')}</td><td>{h.get('journey','-')}</td></tr>"
        for h in HISTORY) or "<tr><td colspan=4 style='color:#888'>No PODs yet — send one on WhatsApp</td></tr>"
    keyset = "set" if os.environ.get("OPENAI_API_KEY") else "MISSING"
    twset = "set" if os.environ.get("TWILIO_ACCOUNT_SID") else "MISSING"
    return f"""<!doctype html><meta charset=utf-8>
<body style="font-family:system-ui;background:#0B2545;color:#fff;padding:30px">
<h2>ePOD Guardian — WhatsApp bot</h2>
<p style="color:#9DB2CE">OPENAI_API_KEY: {keyset} &nbsp;|&nbsp; TWILIO_ACCOUNT_SID: {twset}</p>
<p style="color:#9DB2CE">Send a POD photo to your Twilio WhatsApp sandbox number. Verdicts appear below.</p>
<table style="width:100%;border-collapse:collapse;margin-top:16px;background:#fff;color:#12233B;border-radius:8px;overflow:hidden">
<tr style="background:#F5A623;text-align:left"><th style="padding:10px">Time</th><th>Waybill</th><th>Verdict</th><th>Journey</th></tr>
{rows}</table>
<p style="color:#9DB2CE;margin-top:20px;font-size:13px">Known waybills: {", ".join(j['waybill_number'] for j in JOURNEYS)}</p>
</body>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8010))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
