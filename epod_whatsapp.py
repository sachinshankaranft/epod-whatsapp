"""
ePOD Guardian - WhatsApp bot (single file)  |  FreightTiger hackathon
STEP 1: guided multilingual flow + AI translation + detailed logging

FLOW (metro-ticket style):
  Driver sends anything -> welcome + language menu (reply 1-6)
  Driver picks language -> bot confirms + asks for POD photo (in their language)
  Driver sends photo -> AI reads + validates -> verdict replied in their language

Keys come from environment: OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
Update path: edit this file on GitHub -> Render auto-redeploys.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from datetime import datetime

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

# ============================================================================
# LOGGING - verbose, so failures are visible in Render logs
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("epod")


def logstep(msg):
    log.info(msg)


# ============================================================================
# JOURNEY DATA
# ============================================================================
JOURNEYS = [
    {"journey_fteid": "JRN-JBP-1889", "waybill_number": "6007 0471 8894", "invoice_number": None,
     "consignor_name": "Tata Motors Ltd", "transporter_name": "Safexpress",
     "consignee_name": "Frontier Trucks Pvt Ltd (Jabalpur)", "total_invoice_value": 36335,
     "materials": [{"material_code": "M001", "description": "Auto components"}], "rate_card": {"M001": 380}},
    {"journey_fteid": "JRN-JBP-8894", "waybill_number": "6907 0473 8894", "invoice_number": None,
     "consignor_name": "Tata Motors Ltd", "transporter_name": "Safexpress",
     "consignee_name": "Frontier Trucks (Jabalpur)", "total_invoice_value": 36335,
     "materials": [{"material_code": "M001", "description": "Auto components"}], "rate_card": {"M001": 380}},
    {"journey_fteid": "JRN-RJP-0354", "waybill_number": "7007 0446 0354", "invoice_number": None,
     "consignor_name": "TML Commercial Vehicles Limited", "transporter_name": "Safexpress",
     "consignee_name": "Libra Automotors (Patiala)", "total_invoice_value": 1041,
     "materials": [{"material_code": "M001", "description": "Auto components"}], "rate_card": {"M001": 380}},
    {"journey_fteid": "JRN-PTNA-2299", "waybill_number": "5007 0496 2299", "invoice_number": None,
     "consignor_name": "Tata Motors Limited", "transporter_name": "Safexpress",
     "consignee_name": "Binay Motors Private Limited (Patna)", "total_invoice_value": 76000,
     "materials": [{"material_code": "M001", "description": "Auto components"}], "rate_card": {"M001": 380}},
    # --- JSW One invoices (matched by invoice number) ---
    {"journey_fteid": "JRN-JSW-3544", "waybill_number": None, "invoice_number": "JODLMH0326/43544",
     "consignor_name": "JSW One Distribution Limited", "transporter_name": "JSW One",
     "consignee_name": "Eco-Weld Global Equipments LLP (Dehugaon)", "total_invoice_value": 2121669,
     "materials": [{"material_code": "HRS", "description": "Mild Steel Hot Rolled Sheet"}], "rate_card": {"HRS": 57500}},
    {"journey_fteid": "JRN-JSW-1796", "waybill_number": None, "invoice_number": "JODLMH0226/41796",
     "consignor_name": "JSW One Distribution Limited", "transporter_name": "JSW One",
     "consignee_name": "Nimbai Laser Work (Pune)", "total_invoice_value": 1496337,
     "materials": [{"material_code": "CRS", "description": "Mild Steel Cold Rolled Sheet"}], "rate_card": {"CRS": 62100}},
]
CRITICAL = ["waybill", "lr", "invoice", "number", "material", "signature", "stamp", "damage"]

# ============================================================================
# LANGUAGES
# ============================================================================
LANGUAGES = {
    "1": ("English", "English"),
    "2": ("Hindi", "हिंदी"),
    "3": ("Tamil", "தமிழ்"),
    "4": ("Kannada", "ಕನ್ನಡ"),
    "5": ("Telugu", "తెలుగు"),
    "6": ("Marathi", "मराठी"),
}

WELCOME = (
    "👋 *Welcome to ePOD Guardian* (FreightTiger)\n"
    "Please choose your language / अपनी भाषा चुनें:\n\n"
    "1. English\n"
    "2. हिंदी (Hindi)\n"
    "3. தமிழ் (Tamil)\n"
    "4. ಕನ್ನಡ (Kannada)\n"
    "5. తెలుగు (Telugu)\n"
    "6. मराठी (Marathi)\n\n"
    "_Reply with a number (1-6)_"
)

# English base strings; translated on the fly for other languages
STRINGS_EN = {
    "ask_photo": ("✅ Language set to English.\n\n"
                  "📷 Now please send a *photo of the signed POD / waybill*.\n"
                  "Tap the 📎 attach icon below and take or upload a clear photo of the full document."),
    "ask_waybill": ("✅ Language set to English.\n\n"
                    "Please type the *LR / Waybill number* for this delivery (the 12-digit number on the document, e.g. 6007 0471 8894)."),
    "not_found": ("❌ No open delivery found for that number. Please check the waybill and type it again."),
    "multi_delivery": ("📑 It looks like you've sent paperwork for *more than one delivery* at once. "
                       "Please send *one delivery at a time* — the POD pages for a single waybill. Thank you!"),
    "invalid_pod": ("❌ This doesn't look like a valid ePOD for an open delivery.\n\n"
                    "Please submit a *clear photo of the signed & stamped POD or tax invoice* for your delivery. "
                    "Make sure the waybill / invoice number is visible."),
    "reading": "⏳ Reading your document, please wait…",
}


def confirm_message_en(journey):
    val = ("₹%s" % format(journey["total_invoice_value"], ",")) if journey.get("total_invoice_value") else "-"
    return ("✅ *Delivery found:*\n\n"
            f"Waybill: *{journey['waybill_number']}*\n"
            f"Consignor: {journey['consignor_name']}\n"
            f"Consignee: {journey['consignee_name']}\n"
            f"Transporter: {journey['transporter_name']}\n"
            f"Invoice Value: {val}\n\n"
            "If this is correct, please *send the POD photo* now. 📷")

# per-driver state: {phone: {"lang": "1", "lang_name": "English"}}
SESSIONS: dict[str, dict] = {}
# gate-in triggers: {phone: journey_dict} - set when a gate-in fires, so the
# NEXT photo from that driver maps straight to this journey (no waybill guessing)
TRIGGERED: dict[str, dict] = {}
# processed POD history for the dashboard
HISTORY: list[dict] = []

# the driver's WhatsApp number for the demo (gate-in messages send here)
DRIVER_NUMBER = os.environ.get("DRIVER_NUMBER", "whatsapp:+919110844592")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")


def journey_ident(journey):
    """Display identifier: waybill number if present, else invoice number."""
    if not journey:
        return "-"
    return journey.get("waybill_number") or journey.get("invoice_number") or "-"


def norm(v):
    return "" if v is None else "".join(c for c in str(v).upper() if c.isalnum())


def find_journey(wb, inv=None):
    """Match a journey by waybill number OR invoice number (either identifier)."""
    nwb = norm(wb)
    ninv = norm(inv)
    for j in JOURNEYS:
        if nwb and j.get("waybill_number") and norm(j["waybill_number"]) == nwb:
            return j
        if ninv and j.get("invoice_number") and norm(j["invoice_number"]) == ninv:
            return j
    return None


# ============================================================================
# Validation engine
# ============================================================================
def run_engine(ocr, journey):
    checks, reasons = [], []
    def add(c, s, r=None): checks.append({"check": c, "status": s, "reason": r})
    illeg = [f for f in (ocr.get("illegible_fields") or []) if any(k in str(f).lower() for k in CRITICAL)]
    if illeg:
        r = "Document not readable: " + ", ".join(illeg) + ". Retake with the full document in frame and good lighting."
        add("Legibility", "FAIL", r); return {"verdict": "REJECTED", "checks": checks, "reasons": [r], "shortage": None}
    add("Legibility", "PASS")
    if journey is None:
        r = "Waybill number doesn't match any open delivery. Check you sent the correct POD."
        add("Waybill match", "FAIL", r); return {"verdict": "REJECTED", "checks": checks, "reasons": [r], "shortage": None}
    add("Waybill match", "PASS")
    if not ocr.get("damage_or_shortage"):
        add("Condition (damage / shortage)", "PASS", "No damage or shortage found")
        return {"verdict": "AUTO_APPROVED", "checks": checks, "reasons": [], "shortage": None}
    notes = ocr.get("damage_notes") or []
    r = "Damage/shortage recorded: " + "; ".join(notes) + "." if notes else "Damage/shortage indicated."
    add("Condition (damage / shortage)", "FAIL", r)
    return {"verdict": "PENDING_L1", "checks": checks, "reasons": [r],
            "shortage": {"items": ocr.get("shortage_items") or [], "notes": notes}}


def eval_debit(items, rate_card):
    val = 0.0
    for it in items or []:
        rate, qty = rate_card.get(it.get("material_code")), it.get("shortage_qty") or 0
        if rate and qty: val += rate * qty
    return {"amount": val or None}


# ============================================================================
# OpenAI - vision + translation
# ============================================================================
def _openai_key():
    k = os.environ.get("OPENAI_API_KEY")
    if not k:
        raise RuntimeError("OPENAI_API_KEY not set")
    return k


VISION_PROMPT = """Validate this POD / Safexpress waybill. Return ONLY JSON:
{"waybill_number": string|null, "waybill_confidence": number, "legible": boolean, "illegible_fields": string[], "damage_or_shortage": boolean, "damage_notes": string[], "shortage_items": [{"material_code": string, "shortage_qty": number}], "signature_present": boolean, "stamp_present": boolean}

READING THE WAYBILL NUMBER (most important - be careful):
- It is a 12-digit number formatted as 4-4-4, like "6007 0471 8894".
- It appears TWICE: (a) in a box near the top labelled "Waybill No.", and (b) as the human-readable digits printed directly UNDER the barcode (top right). The digits under the barcode are usually the cleanest, machine-printed copy - prefer those.
- Read BOTH copies and cross-check them against each other. If they agree, you are confident. If they disagree, pick the barcode digits and lower your confidence.
- Watch out for 0/6, 4/6, 1/7, 8/3 confusion. Look carefully at each digit.
- Set "waybill_confidence" 0.0-1.0: use >=0.9 only if both copies clearly agree; 0.5-0.8 if slightly unsure or blurry; <0.5 if you are guessing.

LEGIBILITY: only critical fields matter (waybill number, signature/stamp/damage region). Minor fields (DIM, weight, dates) never count. Normal phone blur is fine.
DAMAGE/SHORTAGE: damage_or_shortage=true only if there's a handwritten/printed note of damage, breakage, shortage, or missing quantity. A normal signature+stamp with no such note = false. Use false/null when unsure."""


VISION_PROMPT_MULTI = """You are validating a Proof of Delivery submission. It may be a single image, MULTIPLE images, or a PDF with several pages. The items are given IN ORDER (index 0, 1, 2, ...). Together they are ONE delivery's paperwork (e.g. front/back or multiple pages of the same waybill).

Look across ALL pages/images and return ONLY JSON:
{"waybill_number": string|null, "invoice_number": string|null, "waybill_confidence": number, "signed_page_index": integer, "legible": boolean, "illegible_fields": string[], "damage_or_shortage": boolean, "damage_notes": string[], "shortage_items": [{"material_code": string, "shortage_qty": number}], "signature_present": boolean, "stamp_present": boolean, "multiple_deliveries": boolean, "is_valid_pod": boolean}

"waybill_number": a 12-digit transporter waybill (4-4-4 format like "6007 0471 8894") if present, else null.
"invoice_number": the tax-invoice / document number if present (e.g. "JODLMH0326/43544"), else null. Look near labels like "Invoice No." / "Invoice Number".
"is_valid_pod": true if this looks like a genuine delivery proof (a waybill or a tax invoice, ideally with a signature/stamp). false if it is something else (a random photo, a weighbridge slip, an unrelated document, or unreadable).
"signed_page_index": 0-based index of the page/image best showing the signed & stamped document. If one item, use 0.
"multiple_deliveries": true ONLY if pages show DIFFERENT waybill/invoice numbers for DIFFERENT deliveries.

READING THE WAYBILL NUMBER (be careful):
- 12 digits formatted 4-4-4, like "6007 0471 8894". It appears near a "Waybill No." box and as digits under the barcode - the barcode digits are cleanest, prefer them. Cross-check both copies.
- Watch 0/6, 4/6, 1/7, 8/3 confusion. Set waybill_confidence 0.0-1.0 (>=0.9 only if both copies agree).

LEGIBILITY: only critical fields matter (waybill number, signature/stamp/damage). Minor fields never count. Normal phone blur is fine.
DAMAGE/SHORTAGE: true only if there's a note of damage/breakage/shortage/missing qty. Normal signature+stamp with no such note = false."""


def extract_with_openai(image_bytes, mtype):
    logstep(f"OCR: calling OpenAI vision ({len(image_bytes)} bytes, {mtype})")
    b64 = base64.b64encode(image_bytes).decode()
    res = requests.post("https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {_openai_key()}", "Content-Type": "application/json"},
        json={"model": "gpt-4o", "max_tokens": 1000, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mtype};base64,{b64}"}}]}]}, timeout=60)
    if res.status_code != 200:
        logstep(f"OCR ERROR: OpenAI returned {res.status_code}: {res.text[:200]}")
        res.raise_for_status()
    txt = res.json()["choices"][0]["message"]["content"]
    parsed = json.loads(txt.replace("```json", "").replace("```", "").strip())
    logstep(f"OCR result: waybill={parsed.get('waybill_number')} confidence={parsed.get('waybill_confidence')} damage={parsed.get('damage_or_shortage')} legible={parsed.get('legible')}")
    return parsed


def extract_multi(attachments):
    """Unified reader: attachments is a list of (bytes, content_type).
    Images and PDFs are sent together as ONE document. OpenAI reads across all
    pages/images, extracts the waybill + signature/damage, tells us which
    page/image holds the signed waybill, and flags if it looks like multiple
    different deliveries. Returns (parsed_json, best_index)."""
    logstep(f"OCR: multi-attachment read, {len(attachments)} item(s): "
            + ", ".join(ct for _, ct in attachments))
    content = [{"type": "text", "text": VISION_PROMPT_MULTI}]
    for i, (data, ct) in enumerate(attachments):
        b64 = base64.b64encode(data).decode()
        if "pdf" in ct.lower():
            content.append({"type": "file", "file": {
                "filename": f"pod_{i}.pdf", "file_data": f"data:application/pdf;base64,{b64}"}})
        else:
            content.append({"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b64}"}})
    res = requests.post("https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {_openai_key()}", "Content-Type": "application/json"},
        json={"model": "gpt-4o", "max_tokens": 1200, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": content}]},
        timeout=120)
    if res.status_code != 200:
        logstep(f"OCR ERROR: OpenAI returned {res.status_code}: {res.text[:300]}")
        res.raise_for_status()
    txt = res.json()["choices"][0]["message"]["content"]
    parsed = json.loads(txt.replace("```json", "").replace("```", "").strip())
    best = parsed.get("signed_page_index")
    if not isinstance(best, int) or best < 0 or best >= len(attachments):
        best = 0
    logstep(f"OCR multi: waybill={parsed.get('waybill_number')} conf={parsed.get('waybill_confidence')} "
            f"signed_page={parsed.get('signed_page_index')} multi_delivery={parsed.get('multiple_deliveries')} "
            f"damage={parsed.get('damage_or_shortage')}")
    return parsed, best


def translate(text, lang_name):
    """Translate English text to the target language. English passes through."""
    if lang_name == "English":
        return text
    try:
        logstep(f"TRANSLATE: -> {lang_name}")
        res = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {_openai_key()}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "max_tokens": 800, "messages": [
                {"role": "system", "content": f"Translate the user's message into {lang_name}. Keep emojis, numbers, waybill IDs, and *bold* markers exactly. Return only the translation, nothing else."},
                {"role": "user", "content": text}]}, timeout=30)
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"].strip()
        logstep(f"TRANSLATE ERROR {res.status_code}: {res.text[:150]}")
    except Exception as e:
        logstep(f"TRANSLATE EXCEPTION: {e}")
    return text  # fall back to English on any failure


# ============================================================================
# Verdict message (English base)
# ============================================================================
def verdict_message_en(result, journey, debit):
    ident = journey_ident(journey)
    if result["verdict"] == "AUTO_APPROVED":
        return (f"✅ *POD ACCEPTED*\n\n"
                f"Ref: {ident}\n"
                f"Consignee: {journey['consignee_name']}\n"
                f"Clean delivery — no damage found.\n"
                f"Billing is now unlocked. You're all set, thank you!")
    if result["verdict"] == "PENDING_L1":
        amt = (f"₹{debit['amount']:,.0f}" if debit and debit.get("amount") else "to be confirmed")
        return (f"⚠️ *POD RECEIVED — UNCLEAN*\n\n"
                f"Ref: {ident}\n"
                f"{result['reasons'][0]}\n"
                f"This will go for L1 approval. A debit note ({amt}) may be raised. Thank you for reporting it.")
    return (f"❌ *POD REJECTED*\n\n{result['reasons'][0]}\n\nPlease retake and send again.")


# ============================================================================
# Twilio media download + reply
# ============================================================================
def download_twilio_media(media_url):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    logstep(f"MEDIA: downloading from Twilio")
    r = requests.get(media_url, auth=(sid, token), timeout=60)
    if r.status_code != 200:
        logstep(f"MEDIA ERROR: Twilio returned {r.status_code}")
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "image/jpeg")


def twiml_reply(message):
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return PlainTextResponse(
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>',
        media_type="application/xml")


def send_whatsapp(to, message):
    """Proactively SEND a WhatsApp message (not a reply) via Twilio REST API.
    Works in the sandbox as long as the recipient's 24h session window is open."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        logstep("SEND ERROR: Twilio credentials not set")
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    logstep(f"SEND: proactive message to {to}")
    r = requests.post(url, auth=(sid, token),
                      data={"From": TWILIO_WHATSAPP_FROM, "To": to, "Body": message}, timeout=30)
    if r.status_code >= 300:
        logstep(f"SEND ERROR {r.status_code}: {r.text[:300]}")
        return False
    logstep(f"SEND OK: {r.status_code}")
    return True


def gatein_message(journey):
    """Safe, language-neutral gate-in prompt + language menu."""
    ident = journey.get("waybill_number") or journey.get("invoice_number") or "-"
    return (
        f"🚚 *You have reached {journey['consignee_name']}*\n"
        f"Delivery LR / Invoice: *{ident}*\n\n"
        f"Please upload the *signed & stamped POD* for this delivery.\n\n"
        f"First, choose your language / अपनी भाषा चुनें:\n"
        f"1. English\n2. हिंदी\n3. தமிழ்\n4. ಕನ್ನಡ\n5. తెలుగు\n6. मराठी\n\n"
        f"_Reply with a number (1-6), then send the POD photo._"
    )


# ============================================================================
# App
# ============================================================================
app = FastAPI()


@app.post("/whatsapp")
async def whatsapp(request: Request):
    form = await request.form()
    sender = form.get("From", "")
    body = (form.get("Body") or "").strip()
    num_media = int(form.get("NumMedia", 0))
    logstep(f"INBOUND from {sender} | media={num_media} | body={body!r}")

    session = SESSIONS.get(sender)
    has_trigger = sender in TRIGGERED

    # --- A photo / PDF / multiple attachments came in ---
    if num_media > 0:
        if not session:
            logstep(f"{sender}: media before language choice, defaulting English")
            session = {"lang": "1", "lang_name": "English"}
            SESSIONS[sender] = session
        lang_name = session["lang_name"]
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "from": sender, "lang": lang_name}
        try:
            # gather ALL attachments (MediaUrl0..N), not just the first
            attachments = []
            for i in range(num_media):
                murl = form.get(f"MediaUrl{i}")
                mct = form.get(f"MediaContentType{i}", "image/jpeg")
                if not murl:
                    continue
                data, ctype = download_twilio_media(murl)
                attachments.append((data, ctype))
            logstep(f"{sender}: gathered {len(attachments)} attachment(s)")
            if not attachments:
                return twiml_reply(translate("⚠️ I didn't receive the file. Please resend the POD.", lang_name))

            ocr, best = extract_multi(attachments)

            # graceful guard: looks like several different deliveries in one message
            if ocr.get("multiple_deliveries"):
                logstep(f"{sender}: multiple_deliveries flagged -> asking one at a time")
                return twiml_reply(translate(STRINGS_EN["multi_delivery"], lang_name))

            # Mapping priority: gate-in trigger > driver-typed waybill > read waybill/invoice
            triggered_journey = TRIGGERED.pop(sender, None)
            typed_journey = session.get("journey")
            if triggered_journey:
                journey = triggered_journey
                logstep(f"MAP: gate-in journey {journey['journey_fteid']}")
            elif typed_journey:
                journey = typed_journey
                logstep(f"MAP: driver-typed journey {journey['journey_fteid']}")
            else:
                journey = find_journey(ocr.get("waybill_number"), ocr.get("invoice_number"))
                logstep(f"MAP: cold, wb={ocr.get('waybill_number')} inv={ocr.get('invoice_number')} "
                        f"-> {journey['journey_fteid'] if journey else 'NO MATCH'}")

            # Invalid-ePOD guard (cold path only - trigger/typed always have a journey):
            # if it's not a valid POD, or matches no open delivery, ask for a valid ePOD.
            if not triggered_journey and not typed_journey:
                if not ocr.get("is_valid_pod", True) or journey is None:
                    logstep(f"{sender}: invalid/unmatched ePOD (valid={ocr.get('is_valid_pod')}, matched={journey is not None})")
                    entry.update({"waybill": ocr.get("waybill_number") or ocr.get("invoice_number") or "-",
                                  "verdict": "INVALID", "reason": "Not a valid ePOD or no matching open delivery.",
                                  "source": "WhatsApp"})
                    HISTORY.insert(0, entry)
                    return twiml_reply(translate(STRINGS_EN["invalid_pod"], lang_name))

            result = run_engine(ocr, journey)
            debit = eval_debit(result["shortage"]["items"], journey["rate_card"]) if (result["verdict"] == "PENDING_L1" and journey) else None
            msg_en = verdict_message_en(result, journey, debit) if (journey or result["verdict"] != "REJECTED") \
                else verdict_message_en(result, {"waybill_number": ocr.get("waybill_number")}, None)
            msg = translate(msg_en, lang_name)
            # store the signed page image for the dashboard
            sdata, sct = attachments[best]
            img_b64 = "data:%s;base64,%s" % (sct, base64.b64encode(sdata).decode())
            wb_shown = journey_ident(journey) if journey else (ocr.get("waybill_number") or ocr.get("invoice_number"))
            entry.update({"waybill": wb_shown, "verdict": result["verdict"],
                          "journey": journey["journey_fteid"] if journey else None,
                          "consignor": journey["consignor_name"] if journey else "-",
                          "consignee": journey["consignee_name"] if journey else "-",
                          "transporter": journey["transporter_name"] if journey else "Unknown",
                          "invoice_value": journey["total_invoice_value"] if journey else None,
                          "debit": debit["amount"] if debit else None,
                          "reason": result["reasons"][0] if result["reasons"] else "",
                          "pages": len(attachments), "signed_page": best + 1,
                          "image": img_b64, "source": "WhatsApp"})
            HISTORY.insert(0, entry)
            session.pop("journey", None)
            session["stage"] = "done"
            logstep(f"VERDICT: {result['verdict']} for {wb_shown} (page {best+1}/{len(attachments)}) -> {lang_name}")
            return twiml_reply(msg)
        except Exception as e:
            logstep(f"ERROR processing media: {type(e).__name__}: {e}")
            entry.update({"verdict": "ERROR", "error": str(e)})
            HISTORY.insert(0, entry)
            return twiml_reply(translate("⚠️ Sorry, I couldn't read that document. Please retake it clearly and send again.", lang_name))

    # --- A language choice (1-6) ---
    if body in LANGUAGES:
        eng_name, native = LANGUAGES[body]
        # both gate-in and driver-initiated now go straight to asking for the photo
        SESSIONS[sender] = {"lang": body, "lang_name": eng_name, "stage": "await_photo"}
        logstep(f"{sender}: language {eng_name} -> ask photo")
        return twiml_reply(translate(STRINGS_EN["ask_photo"], eng_name))

    # --- Anything else -> welcome/menu ---
    logstep(f"{sender}: showing welcome menu")
    return twiml_reply(WELCOME)


@app.post("/gatein/{idx}")
async def gatein(idx: int):
    """Simulate a truck reaching the consignee gate-in for JOURNEYS[idx].
    Fires a proactive WhatsApp to the driver and arms the mapping."""
    if idx < 0 or idx >= len(JOURNEYS):
        return PlainTextResponse("bad journey index", status_code=400)
    journey = JOURNEYS[idx]
    logstep(f"GATE-IN triggered for {journey['journey_fteid']} ({journey['consignee_name']})")
    TRIGGERED[DRIVER_NUMBER] = journey
    SESSIONS.pop(DRIVER_NUMBER, None)  # force language menu fresh
    ok = send_whatsapp(DRIVER_NUMBER, gatein_message(journey))
    return PlainTextResponse("sent" if ok else "failed (check 24h window / creds)",
                             status_code=200 if ok else 500)


@app.get("/", response_class=HTMLResponse)
def home():
    import json as _json
    # Seed rows (modeled on the FT Settlement screen) + live WhatsApp submissions on top.
    seed = [
        {"time": "seed", "waybill": "FTI2", "verdict": "REJECTED", "transporter": "ProtoShip Logistics",
         "consignor": "FT Product Demo", "consignee": "ProtoFreight Consignees",
         "route_from": "315, Work Avenue, GK Co...", "route_to": "Scion Enclave, Govindapp...",
         "material": "Tyre 100", "invoice_value": None, "debit": None,
         "reason": "POD rejected by transporter — delivery rejected.", "source": "Driver App", "image": None,
         "so": "SO-123", "do": "DO-123", "invoice": "INV-10-123-2025", "vehicle": "KA03AB2341"},
        {"time": "seed", "waybill": "6907 0473 8894", "verdict": "AUTO_APPROVED", "transporter": "Safexpress",
         "consignor": "Tata Motors Ltd", "consignee": "Frontier Trucks (Jabalpur)",
         "route_from": "Nagpur", "route_to": "Jabalpur-11", "material": "Auto components",
         "invoice_value": 36335, "debit": None, "reason": "", "source": "WhatsApp", "image": None},
        {"time": "seed", "waybill": "7007 0446 0354", "verdict": "PENDING_L1", "transporter": "Safexpress",
         "consignor": "TML Commercial Vehicles Ltd", "consignee": "Libra Automotors (Patiala)",
         "route_from": "WBS001", "route_to": "Rajpura-11", "material": "Auto components",
         "invoice_value": 1041, "debit": 760, "reason": "Damage/shortage recorded: 2 units short — carton torn.",
         "source": "WhatsApp", "image": None},
    ]
    rows = HISTORY + seed

    # status card counts
    n_pending = sum(1 for r in rows if r.get("verdict") == "PENDING_SUBMISSION")
    n_submitted = sum(1 for r in rows if r.get("verdict") in ("AUTO_APPROVED", "PENDING_L1"))
    n_rejected = sum(1 for r in rows if r.get("verdict") == "REJECTED")
    n_approved = sum(1 for r in rows if r.get("verdict") == "AUTO_APPROVED")
    n_clean = sum(1 for r in rows if r.get("verdict") == "AUTO_APPROVED")
    n_unclean = sum(1 for r in rows if r.get("verdict") == "PENDING_L1")

    data_json = _json.dumps(rows)
    journeys_json = _json.dumps([{"consignee_name": j["consignee_name"],
                                  "waybill_number": j.get("waybill_number") or j.get("invoice_number") or "-"} for j in JOURNEYS])
    return DASHBOARD_HTML.replace("__ROWS__", data_json).replace("__JOURNEYS__", journeys_json)\
        .replace("__PENDING__", str(n_pending)).replace("__SUBMITTED__", str(n_submitted))\
        .replace("__REJECTED__", str(n_rejected)).replace("__APPROVED__", str(n_approved))\
        .replace("__CLEAN__", str(n_clean)).replace("__UNCLEAN__", str(n_unclean))\
        .replace("__COUNT__", str(len(rows)))


DASHBOARD_HTML = r"""<!doctype html><meta charset=utf-8><title>Settlement · ePOD Guardian</title>
<style>
*{box-sizing:border-box;font-family:'Segoe UI',system-ui,sans-serif;margin:0}
body{background:#F7F8FA;color:#1a2332}
.top{background:#fff;border-bottom:1px solid #E3E8EF;padding:20px 32px}
.top h1{font-size:22px;font-weight:600}
.tabs{display:flex;gap:28px;padding:0 32px;background:#fff;border-bottom:1px solid #E3E8EF}
.tab{padding:14px 2px;font-size:15px;color:#5C6B7E;cursor:pointer;border-bottom:2px solid transparent}
.tab.active{color:#2563EB;border-bottom-color:#2563EB;font-weight:600}
.tab .badge{background:#EEF2F7;border-radius:10px;padding:1px 8px;font-size:12px;margin-left:6px}
.wrap{padding:24px 32px}
.filterbar{margin-bottom:18px}
.tdrop{border:1px solid #E3E8EF;border-radius:8px;padding:10px 16px;background:#fff;color:#5C6B7E;display:inline-block;min-width:220px;font-size:14px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.card{background:#fff;border:1px solid #E3E8EF;border-radius:10px;padding:18px 20px}
.card .n{font-size:26px;font-weight:600}
.card .l{font-size:14px;color:#5C6B7E;margin-top:2px}
.card.rej .n,.card.rej .l{color:#2563EB}
.card.appr{position:relative}
.subrow{display:flex;gap:16px;margin-top:12px;background:#F7F8FA;border-radius:8px;padding:8px 12px;font-size:13px;color:#5C6B7E}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #E3E8EF;border-radius:10px;overflow:hidden}
th{text-align:left;padding:14px 18px;font-size:13px;color:#5C6B7E;font-weight:600;border-bottom:1px solid #E3E8EF}
td{padding:16px 18px;font-size:14px;border-bottom:1px solid #F0F2F5;vertical-align:top}
tr:last-child td{border-bottom:none}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:16px;font-size:12px;font-weight:600}
.pill.ok{background:#E7F3EC;color:#1D7D46}.pill.warn{background:#FDF2E2;color:#B3720F}.pill.rej{background:#FBE9E8;color:#B3261E}
.src{display:inline-block;font-size:11px;background:#EEF2F7;color:#5C6B7E;border-radius:6px;padding:2px 7px;margin-top:4px}
.src.wa{background:#E7F5EC;color:#128C4B}
.viewbtn{border:1px solid #2563EB;color:#2563EB;background:#fff;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}
.route{font-size:13px;color:#5C6B7E;line-height:1.5}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
/* slide panel */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;z-index:10}
.panel{position:fixed;top:0;right:0;width:640px;max-width:92vw;height:100%;background:#fff;overflow-y:auto;
transform:translateX(100%);transition:transform .25s;z-index:11;padding:26px 30px}
.panel.open{transform:translateX(0)}.overlay.open{display:block}
.pclose{float:right;font-size:22px;color:#5C6B7E;cursor:pointer;border:none;background:none}
.rbanner{background:#FBE9E8;border-radius:8px;padding:12px 14px;color:#B3261E;font-weight:600;margin:14px 0;display:flex;align-items:center;gap:8px}
.wbanner{background:#FDF2E2;color:#B3720F}
.gbanner{background:#E7F3EC;color:#1D7D46}
.pgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin:18px 0}
.pgrid .lab{font-size:12px;color:#8A97A8}.pgrid .val{font-size:14px;font-weight:500;margin-top:2px}
.podimg{width:100%;border:1px solid #E3E8EF;border-radius:10px;margin-top:10px}
.aibox{background:#F0F6FF;border:1px solid #CFE0FA;border-radius:10px;padding:14px;margin-top:16px}
.aibox .h{font-size:12px;font-weight:700;color:#2563EB;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px}
</style>
<div class="top"><h1>Settlement <span style="font-size:13px;color:#8A97A8;font-weight:400">· ePOD Guardian (AI-validated via WhatsApp)</span></h1></div>
<div class="tabs">
  <div class="tab active">POD <span class="badge">__COUNT__</span></div>
  <div class="tab">Freight Invoices</div>
  <div class="tab">Debit Notes</div>
</div>
<div class="wrap">
  <div class="filterbar"><span class="tdrop">All Transporter ▾</span></div>
  <div class="cards">
    <div class="card"><div class="n">__PENDING__</div><div class="l">Pending Submission</div></div>
    <div class="card"><div class="n">__SUBMITTED__</div><div class="l">Submitted</div></div>
    <div class="card rej"><div class="n">__REJECTED__ ⊘</div><div class="l">Rejected</div></div>
    <div class="card appr"><div class="n">__APPROVED__ Approved</div>
      <div class="subrow"><span><b>__CLEAN__</b> Clean Delivery</span><span>|</span><span><b>__UNCLEAN__</b> Unclean Delivery</span></div></div>
  </div>
  <div style="margin-bottom:12px;font-size:14px;color:#5C6B7E">__COUNT__ Trips available</div>
  <div id="gatein" style="background:#fff;border:1px solid #E3E8EF;border-radius:10px;padding:16px 18px;margin-bottom:18px">
    <div style="font-size:13px;font-weight:700;color:#2563EB;text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px">🚚 Simulate Gate-In (proactive trigger)</div>
    <div style="font-size:13px;color:#5C6B7E;margin-bottom:12px">In production this fires automatically when the truck geofences into the consignee. Click to send the driver a proactive "upload your POD" WhatsApp for that delivery.</div>
    <div id="gbtns" style="display:flex;gap:10px;flex-wrap:wrap"></div>
    <div id="gmsg" style="font-size:13px;margin-top:10px;color:#5C6B7E"></div>
  </div>
  <table>
    <thead><tr><th>LR / Waybill</th><th>Transporter</th><th>Route</th><th>Material</th><th>Approval Status</th><th>Actions</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>
<div class="overlay" id="ov" onclick="closePanel()"></div>
<div class="panel" id="panel"></div>
<script>
const DATA = __ROWS__;
const JOURNEYS = __JOURNEYS__;
// gate-in buttons
document.getElementById('gbtns').innerHTML = JOURNEYS.map((j,i)=>
  `<button class="viewbtn" style="border-color:#128C4B;color:#128C4B" onclick="fireGateIn(${i},this)">${j.consignee_name} · ${j.waybill_number}</button>`
).join('');
function fireGateIn(i,btn){
  const m=document.getElementById('gmsg');
  m.textContent='Sending gate-in message to driver…'; btn.disabled=true;
  fetch('/gatein/'+i,{method:'POST'}).then(r=>r.text()).then(t=>{
    m.innerHTML = t==='sent'
      ? '✅ Gate-in message sent to driver on WhatsApp. Reply there with the POD photo — it will map to <b>'+JOURNEYS[i].waybill_number+'</b>.'
      : '⚠️ '+t+' — make sure you\'ve messaged the sandbox in the last 24h.';
    btn.disabled=false;
  }).catch(e=>{m.textContent='Error: '+e;btn.disabled=false;});
}
const VP = {AUTO_APPROVED:['ok','APPROVED'],PENDING_L1:['warn','UNCLEAN · L1'],REJECTED:['rej','REJECTED'],ERROR:['rej','ERROR']};
function pill(v){const p=VP[v]||['warn',v||'—'];return `<span class="pill ${p[0]}">${p[1]}</span>`;}
function routeCell(r){
  const f=r.route_from||r.consignor||'-', t=r.route_to||r.consignee||'-';
  return `<div class="route"><span class="dot" style="background:#1D7D46"></span>${f}<br><span class="dot" style="background:#B3261E"></span>${t}</div>`;}
function srcTag(s){return `<span class="src ${s==='WhatsApp'?'wa':''}">${s||'—'}</span>`;}
document.getElementById('tbody').innerHTML = DATA.map((r,i)=>`
  <tr>
    <td><b>${r.waybill||'-'}</b>${r.verdict==='REJECTED'?'<div style="color:#B3261E;font-size:12px;margin-top:3px">Delivery rejected</div>':''}<br>${srcTag(r.source)}</td>
    <td>${r.transporter||'-'}</td>
    <td>${routeCell(r)}</td>
    <td>${r.material||'-'}</td>
    <td>${pill(r.verdict)}</td>
    <td><button class="viewbtn" onclick='openPanel(${i})'>View ePOD</button></td>
  </tr>`).join('');
function openPanel(i){
  const r=DATA[i];
  let banner='';
  if(r.verdict==='REJECTED') banner=`<div class="rbanner">⊘ POD rejected — AI validation failed</div>`;
  else if(r.verdict==='PENDING_L1') banner=`<div class="rbanner wbanner">⚠ Unclean delivery — pending L1 approval</div>`;
  else if(r.verdict==='AUTO_APPROVED') banner=`<div class="rbanner gbanner">✓ POD auto-approved — billing unlocked</div>`;
  const ai = r.reason ? `<div class="aibox"><div class="h">🤖 AI validation reason</div>${r.reason}${r.debit?`<div style="margin-top:8px;color:#B3261E;font-weight:600">Provisional debit note: ₹${Number(r.debit).toLocaleString('en-IN')}</div>`:''}</div>` : `<div class="aibox"><div class="h">🤖 AI validation</div>All checks passed. Clean delivery, no damage or shortage detected.</div>`;
  const img = r.image ? `<div class="pgrid" style="grid-template-columns:1fr"><div><div class="lab">POD IMAGE (received via ${r.source})</div><img class="podimg" src="${r.image}"></div></div>` : `<div class="aibox" style="background:#F7F8FA;border-color:#E3E8EF;color:#8A97A8">POD image will appear here when sent via WhatsApp.</div>`;
  document.getElementById('panel').innerHTML=`
    <button class="pclose" onclick="closePanel()">✕</button>
    <div style="font-size:18px;font-weight:600">Waybill ${r.waybill||'-'} ${srcTag(r.source)}</div>
    ${banner}
    <div class="pgrid">
      <div><div class="lab">Consignor</div><div class="val">${r.consignor||'-'}</div></div>
      <div><div class="lab">Consignee</div><div class="val">${r.consignee||'-'}</div></div>
      <div><div class="lab">Transporter</div><div class="val">${r.transporter||'-'}</div></div>
      <div><div class="lab">SO</div><div class="val">${r.so||'—'}</div></div>
      <div><div class="lab">DO</div><div class="val">${r.do||'—'}</div></div>
      <div><div class="lab">Invoice</div><div class="val">${r.invoice||'—'}</div></div>
      <div><div class="lab">Invoice Value</div><div class="val">${r.invoice_value?'₹'+Number(r.invoice_value).toLocaleString('en-IN'):'—'}</div></div>
      <div><div class="lab">Vehicle</div><div class="val">${r.vehicle||'—'}</div></div>
      <div><div class="lab">Language</div><div class="val">${r.lang||'—'}</div></div>
    </div>
    ${ai}
    ${img}`;
  document.getElementById('panel').classList.add('open');
  document.getElementById('ov').classList.add('open');
}
function closePanel(){document.getElementById('panel').classList.remove('open');document.getElementById('ov').classList.remove('open');}
</script>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8010))
    logstep(f"Starting ePOD Guardian on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
