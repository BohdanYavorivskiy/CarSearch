"""Rough damage assessment.

Two independent signals are produced for every lot:

1. ``text_damage_score`` (0..1, always available) – derived from the auction's
   own damage codes (primary / secondary damage), condition, keys, airbags and
   the repair-cost / ACV ratio.
2. ``photo_damage`` (optional) – Claude vision looks at up to N photos and
   returns a structured verdict: overall severity 0-10, affected zones,
   structural / airbag / flood / fire signs, a rough repair-cost bucket.
   Needs ``ANTHROPIC_API_KEY`` (or another credential the Anthropic SDK finds).

Both end up in the dataset so the analysis can use whichever is present.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os

log = logging.getLogger(__name__)

# Severity of auction damage codes (Copart / IAAI vocabulary + UA/RU translations).
DAMAGE_SEVERITY: list[tuple[tuple[str, ...], float]] = [
    (("burn", "fire", "пожеж", "пожар", "горі", "гори"), 1.0),
    (("rollover", "перекид", "переворот"), 1.0),
    (("water", "flood", "затоп", "вода", "повінь", "наводн"), 0.9),
    (("all over", "по всьому", "по всему", "кругом"), 0.85),
    (("stripped", "розукомплект", "разукомплект"), 0.8),
    (("frame", "рама"), 0.8),
    (("biohazard", "біо", "био"), 0.6),
    (("front end", "front", "передн", "перед"), 0.55),
    (("undercarriage", "днище", "ходов"), 0.5),
    (("top/roof", "roof", "дах", "крыш"), 0.5),
    (("rear end", "rear", "задн", "зад"), 0.45),
    (("side", "бок", "бiк", "бік"), 0.45),
    (("mechanical", "механічн", "механическ", "engine damage", "transmission damage"), 0.4),
    (("vandalism", "вандал"), 0.3),
    (("hail", "град"), 0.3),
    (("replaced vin", "rejected repair"), 0.3),
    (("partial repair", "часткови", "частичн"), 0.3),
    (("minor dent", "scratch", "подряп", "царап", "вм'ят", "вмятин"), 0.15),
    (("normal wear", "знос", "износ"), 0.1),
    (("theft", "крадіжк", "угон", "кража"), 0.2),
    (("none", "no damage", "немає", "нет"), 0.0),
]


def damage_code_severity(text: str | None) -> float | None:
    if not text:
        return None
    t = text.lower()
    for keys, sev in DAMAGE_SEVERITY:
        if any(k in t for k in keys):
            return sev
    return 0.4  # unknown code: assume moderate


def text_damage_score(lot: dict) -> float:
    """Combine auction metadata into one 0..1 score (higher = worse)."""
    primary = damage_code_severity(lot.get("primary_damage")) or 0.0
    secondary = damage_code_severity(lot.get("secondary_damage")) or 0.0
    score = primary + 0.35 * secondary

    cond = (lot.get("condition") or "").lower()
    if any(w in cond for w in ("stationary", "не заводиться", "не заводится", "won't start", "does not start", "not run")):
        score += 0.15
    elif any(w in cond for w in ("run and drive", "run & drive", "заводиться і їде", "на ходу", "run")):
        score -= 0.1

    keys = (lot.get("keys") or "").lower()
    if keys and any(w in keys for w in ("no", "ні", "нет", "missing")):
        score += 0.05

    airbags = (lot.get("airbags") or "").lower()
    if airbags and any(w in airbags for w in ("deployed", "спрацюв", "сработ", "розкри", "раскры")):
        score += 0.15

    rc, acv = lot.get("repair_cost_usd"), lot.get("acv_usd")
    if rc and acv:
        score = 0.6 * score + 0.4 * min(rc / acv, 1.5) / 1.5

    return round(max(0.0, min(score, 1.0)), 3)


# -------------------------------------------------------------- Claude vision
PHOTO_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "integer", "description": "0 = no visible damage, 10 = total loss / burnt / destroyed"},
        "zones": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["front", "rear", "left_side", "right_side", "roof_top", "interior", "undercarriage", "engine_bay", "wheels_suspension", "soft_top"],
            },
        },
        "structural_damage_suspected": {"type": "boolean"},
        "airbags_deployed": {"type": "boolean"},
        "flood_signs": {"type": "boolean"},
        "fire_signs": {"type": "boolean"},
        "missing_parts": {"type": "boolean"},
        "repair_bucket": {"type": "string", "enum": ["cosmetic", "light", "moderate", "heavy", "total_loss"]},
        "est_repair_usd_low": {"type": "integer"},
        "est_repair_usd_high": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "notes": {"type": "string", "description": "One or two sentences, English."},
    },
    "required": [
        "severity", "zones", "structural_damage_suspected", "airbags_deployed", "flood_signs", "fire_signs",
        "missing_parts", "repair_bucket", "est_repair_usd_low", "est_repair_usd_high", "confidence", "notes",
    ],
    "additionalProperties": False,
}

PROMPT = (
    "You are an experienced auto-body appraiser looking at salvage-auction photos of a Mazda MX-5 (Miata). "
    "Assess only what is visible. Score overall severity 0-10, list the damaged zones, flag structural damage, "
    "deployed airbags, flood or fire signs and missing parts, and give a rough US repair-cost range in USD "
    "(parts + labour at an independent shop). If photos are unclear, say so in notes and lower confidence.\n"
    "Auction metadata (may be wrong): {meta}"
)


def _prepare_image(data: bytes, max_side: int = 1024) -> tuple[str, str] | None:
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()
    except Exception as e:  # corrupt / not an image
        log.debug("skip image: %s", e)
        return None


class PhotoAnalyzer:
    def __init__(self, fetcher, model: str | None = None, max_photos: int = 6):
        import anthropic

        self.client = anthropic.Anthropic()
        self.fetcher = fetcher
        self.model = model or os.environ.get("CARSEARCH_VISION_MODEL", "claude-opus-5-5")
        self.max_photos = max_photos

    def analyze(self, lot: dict) -> dict | None:
        photos = lot.get("photos") or []
        content: list[dict] = []
        for url in photos[: self.max_photos]:
            try:
                prepared = _prepare_image(self.fetcher.get_bytes(url))
            except Exception as e:
                log.warning("photo download failed %s: %s", url, e)
                continue
            if prepared:
                mt, b64 = prepared
                content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
        if not content:
            return None
        meta = {k: lot.get(k) for k in ("year", "primary_damage", "secondary_damage", "condition", "mileage", "airbags")}
        content.append({"type": "text", "text": PROMPT.format(meta=json.dumps(meta, ensure_ascii=False))})

        import anthropic

        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": PHOTO_SCHEMA}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.RateLimitError as e:
            log.warning("rate limited: %s", e)
            return None
        except anthropic.APIStatusError as e:
            log.warning("Claude API error %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.warning("Claude API connection error: %s", e)
            return None

        if resp.stop_reason == "refusal":
            log.warning("photo analysis refused for lot %s", lot.get("lot_id"))
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            log.warning("bad JSON from model for lot %s", lot.get("lot_id"))
            return None
        result["model"] = resp.model
        result["photos_used"] = len(content) - 1
        return result
