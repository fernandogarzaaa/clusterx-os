"""AI lead-qualification agent.

State machine: greet -> needs -> budget -> timeline -> intent -> book/handoff.

Two response paths:
- Local (default): real rule-based slot filling + intent keyword routing. The
  reply is composed from the current slot state, so it differs per state.
- LLM: when an OpenAI-compatible provider is configured, it drafts the reply
  from a compact system prompt (stage + filled slots) and we parse the JSON
  envelope. Any parse or API failure falls back to the local composer.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

STAGES = ["greet", "needs", "budget", "timeline", "intent", "book", "handoff"]
KANBAN_STAGES = ["New", "Contacted", "Qualified", "Booked", "Won", "Lost"]

_BUDGET_RE = re.compile(
    r"(?:\u20b9|\$|rs\.?\s*|inr\s*|usd\s*)([\d,]+(?:\.\d+)?)"
    r"\s*(k|thousand|lakh|lakhs|crore|cr|million|m)?\b"
    r"|([\d,]+(?:\.\d+)?)\s*(k|thousand|lakh|lakhs|crore|cr|million|m)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?")
_TIMELINE_RES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bnext week\b",
        r"\bthis week\b",
        r"\bnext month\b",
        r"\bthis month\b",
        r"\basap\b",
        r"\ba\.s\.a\.p\.?\b",
        r"\bimmediately\b",
        r"\bright away\b",
        r"\bq[1-4]\b(?:\s*\d{4})?",
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b(?:\s+\d{1,2})?",
        r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
        r"\bend of (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
        r"\b(?:early|mid|late)\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    ]
]
_GREETING_ONLY_RE = re.compile(
    r"^(hi|hello|hey|yo|good\s+(morning|afternoon|evening)|namaste)\W*$",
    re.IGNORECASE,
)

# intent keyword buckets; checked in priority order on ties
INTENT_KEYWORDS: dict[str, list[str]] = {
    "Unqualified": [
        "not interested",
        "no thanks",
        "stop",
        "unsubscribe",
        "remove me",
        "wrong number",
        "do not contact",
        "don't contact",
        "leave me alone",
    ],
    "Hot": [
        "send proposal",
        "proposal",
        "demo",
        "pricing",
        "price quote",
        "interested",
        "sign up",
        "signup",
        "let's do it",
        "lets do it",
        "ready to buy",
        "buy",
        "purchase",
        "yes please",
        "book a call",
        "schedule a call",
    ],
    "Warm": [
        "maybe",
        "later",
        "send info",
        "send me info",
        "more info",
        "thinking about",
        "considering",
        "could be",
        "possibly",
        "not yet",
    ],
    "Cold": [
        "not sure",
        "just looking",
        "browsing",
        "just checking",
        "curious",
        "window shopping",
    ],
}
_INTENT_PRIORITY = ["Unqualified", "Hot", "Warm", "Cold"]


@dataclass
class AgentResult:
    reply: str
    slot_updates: dict = field(default_factory=dict)
    agent_stage: str = "greet"
    kanban_stage: str | None = None  # set when the kanban stage should advance
    intent: tuple[str, float, list[str]] | None = None  # (label, conf, reasons)
    events: list[tuple[str, dict]] = field(default_factory=list)


def extract_budget(text: str) -> str | None:
    """Pull a budget amount out of free text. Returns a display string or None."""
    m = _BUDGET_RE.search(text)
    if m:
        raw = (m.group(1) or m.group(3) or "").replace(",", "")
        suffix = (m.group(2) or m.group(4) or "").lower()
        try:
            amount = float(raw)
        except ValueError:
            return None
        scale = {"k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5,
                 "crore": 1e7, "cr": 1e7, "million": 1e6, "m": 1e6}.get(suffix, 1.0)
        amount *= scale
        prefix = m.group(0).lower()
        if "\u20b9" in prefix or "rs" in prefix or "inr" in prefix \
                or suffix in ("lakh", "lakhs", "crore", "cr"):
            cur = "\u20b9"
        else:
            cur = "$"
        return f"{cur}{amount:,.0f}"
    if "budget" in text.lower():
        n = _NUMBER_RE.search(text)
        if n:
            try:
                return f"${float(n.group(0).replace(',', '')):,.0f}"
            except ValueError:
                return None
    return None


def extract_timeline(text: str) -> str | None:
    for rx in _TIMELINE_RES:
        m = rx.search(text)
        if m:
            return m.group(0).strip()
    return None


def extract_need(text: str) -> str | None:
    """Summarize the lead's need from their message (first sentence, trimmed)."""
    cleaned = text.strip()
    if not cleaned or _GREETING_ONLY_RE.match(cleaned):
        return None
    first = re.split(r"[.!?;\n]", cleaned, maxsplit=1)[0].strip()
    first = re.sub(r"^(hi|hello|hey|yo)\b[,.]?\s*", "", first, flags=re.IGNORECASE)
    first = first[:120].strip()
    return first or None


def classify_intent(messages: list[str]) -> tuple[str, float, list[str]]:
    """Keyword-score conversation text into Hot/Warm/Cold/Unqualified."""
    blob = " ".join(messages).lower()
    hits: dict[str, list[str]] = {label: [] for label in _INTENT_PRIORITY}
    for label in _INTENT_PRIORITY:
        for kw in INTENT_KEYWORDS[label]:
            if kw in blob:
                hits[label].append(kw)
    scores = {label: len(v) for label, v in hits.items()}
    total = sum(scores.values())
    if total == 0:
        return "Cold", 0.4, ["no buying signals detected yet"]
    label = max(_INTENT_PRIORITY, key=lambda lab: scores[lab])
    reasons = [f"mentioned '{kw}'" for kw in hits[label][:4]]
    confidence = round(scores[label] / total, 2)
    return label, confidence, reasons


def _compose_reply(slots: dict, target: str, lead_name: str) -> str:
    """Build the agent reply from slot state. Differs per target stage."""
    need = slots.get("need")
    budget = slots.get("budget")
    timeline = slots.get("timeline")
    name = f" {lead_name.split()[0]}" if lead_name else ""

    if target == "needs":
        if need:
            return (
                f"Got it{name}, {need}. To point you at the right option, "
                "what does your current setup look like for this?"
            )
        return (
            f"Hi{name}! Thanks for reaching out. What are you hoping to "
            "solve or improve? A sentence or two is plenty."
        )
    if target == "budget":
        ref = f" for {need}" if need else ""
        return (
            f"Thanks{name}, that helps. Do you have a rough budget range{ref}? "
            "Even a ballpark works."
        )
    if target == "timeline":
        ref = f" with a budget around {budget}" if budget else ""
        return (
            f"Noted{name}{ref}. When are you hoping to have this in place? "
            "Something like 'next month' or 'Q3' is fine."
        )
    if target == "intent":
        return (
            f"Perfect{name}. Last quick one: how are you handling this today, "
            "and what would make you switch?"
        )
    if target == "book":
        bits = []
        if need:
            bits.append(f"need: {need}")
        if budget:
            bits.append(f"budget: {budget}")
        if timeline:
            bits.append(f"timeline: {timeline}")
        summary = "; ".join(bits)
        return (
            f"Thanks{name}! Based on what you shared ({summary}), it looks like "
            "a strong fit. Want me to book a short intro call? Reply with a day "
            "and time that works, or grab a slot on our calendar page."
        )
    # handoff
    return (
        f"No problem{name}, I will step back. If anything changes, just reply "
        "here and a human will pick it up."
    )


def _next_target(slots: dict, agent_stage: str) -> str:
    order = ["needs", "budget", "timeline", "intent", "book"]
    slot_for = {"needs": "need", "budget": "budget", "timeline": "timeline"}
    start = order.index(agent_stage) if agent_stage in order else 0
    for target in order[start:]:
        if target in slot_for and slots.get(slot_for[target]):
            continue
        if target == "intent" and slots.get("intent_asked"):
            return "book"
        return target
    return "book"


def _llm_reply(provider, slots: dict, target: str, history: list[str]) -> str | None:
    """Ask the configured LLM for a reply. Returns None on any failure."""
    system = (
        "You are a friendly B2B lead-qualification assistant. Current stage: "
        f"{target}. Known slots: need={slots.get('need')}, "
        f"budget={slots.get('budget')}, timeline={slots.get('timeline')}. "
        "Reply in compact JSON only: {\"reply\": \"<your message>\"}. "
        "One or two sentences, no emojis."
    )
    convo = "\n".join(history[-6:])
    try:
        raw = provider.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": convo},
        ])
        data = json.loads(raw.strip().strip("`"))
        reply = str(data.get("reply", "")).strip()
        return reply or None
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        logger.warning("agent reply parse failed, returning None: %s", exc)
        return None


def process_inbound(
    lead,
    text: str,
    provider,
    history: list[str] | None = None,
) -> AgentResult:
    """Process one inbound lead message. Returns the agent reply + state updates."""
    try:
        slots = json.loads(lead.slots_json or "{}")
    except json.JSONDecodeError:
        slots = {}
    agent_stage = slots.get("_agent_stage", "greet")
    history = history or []
    result = AgentResult(reply="", agent_stage=agent_stage)
    lowered = text.lower()

    # Hard opt-out / unqualified always wins.
    if any(kw in lowered for kw in INTENT_KEYWORDS["Unqualified"]):
        slots["_agent_stage"] = "handoff"
        result.agent_stage = "handoff"
        result.intent = ("Unqualified", 0.95, ["explicit opt-out language"])
        result.reply = _compose_reply(slots, "handoff", lead.name)
        result.events.append(("intent_classified", {"intent": "Unqualified"}))
        result.events.append(("stage_changed", {"stage": "handoff"}))
        result.slot_updates = slots
        return result

    # Slot filling from this message.
    if agent_stage in ("greet", "needs") and not slots.get("need"):
        need = extract_need(text)
        if need:
            slots["need"] = need
            result.events.append(("slot_filled", {"slot": "need", "value": need}))
    budget = extract_budget(text)
    if budget and not slots.get("budget"):
        slots["budget"] = budget
        result.events.append(("slot_filled", {"slot": "budget", "value": budget}))
    timeline = extract_timeline(text)
    if timeline and not slots.get("timeline"):
        slots["timeline"] = timeline
        result.events.append(("slot_filled", {"slot": "timeline", "value": timeline}))

    target = _next_target(slots, agent_stage)
    if target != agent_stage and agent_stage != "handoff":
        slots["_agent_stage"] = target
        result.agent_stage = target
        result.events.append(("stage_changed", {"from": agent_stage, "to": target}))
    if target == "intent":
        slots["intent_asked"] = True

    # Intent classification over the conversation so far.
    all_text = history + [text]
    label, conf, reasons = classify_intent(all_text)
    result.intent = (label, conf, reasons)
    result.events.append(("intent_classified", {"intent": label, "confidence": conf}))

    # Kanban advancement: first real reply -> Contacted; Hot intent -> Qualified.
    if lead.stage == "New":
        result.kanban_stage = "Contacted"
    elif label == "Hot" and lead.stage in ("New", "Contacted"):
        result.kanban_stage = "Qualified"

    # Reply: LLM when configured, else local composer.
    reply = None
    if getattr(provider, "name", "local") == "openai-compatible":
        reply = _llm_reply(provider, slots, target, all_text)
    if not reply:
        reply = _compose_reply(slots, target, lead.name)
    result.reply = reply
    result.slot_updates = slots
    return result
