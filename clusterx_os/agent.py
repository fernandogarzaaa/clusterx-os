"""Lead-qualification agent.

State machine: greet -> needs -> budget -> timeline -> intent -> book | handoff.

The rule engine is deterministic and always runs: it fills slots (need,
budget, timeline), classifies intent and drafts a reply for the current
stage. When an LLM is configured it rewrites the draft and may refine the
intent and fill slots the rules missed; any LLM failure keeps the rule
result. Opt-outs and booking confirmations are never left to the LLM.

The agent is pure: no database or network access except the injected LLM.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from clusterx_os.integrations.llm import LLMClient
from clusterx_os.models import INTENTS
from clusterx_os.timeparse import parse_requested_time

logger = logging.getLogger(__name__)

STAGES = ["greet", "needs", "budget", "timeline", "intent", "book", "handoff"]
MAX_REPLY_CHARS = 1000

_SCALE = {"k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "cr": 1e7,
          "million": 1e6, "m": 1e6, "mm": 1e6}
_INR_SUFFIXES = ("lakh", "lakhs", "crore", "cr")
_BUDGET_RE = re.compile(
    r"(?:\u20b9|\$|\u20b1|rs\.?\s*|inr\s*|usd\s*|php\s*)([\d,]+(?:\.\d+)?)"
    r"\s*(k|thousand|lakhs|lakh|crore|cr|million|mm|m)?\b"
    r"|([\d,]+(?:\.\d+)?)\s*(k|thousand|lakhs|lakh|crore|cr|million|mm|m)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TIMELINE_RES = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bnext (?:week|month|quarter|year)\b",
        r"\bthis (?:week|month|quarter|year)\b",
        r"\bin (?:\d+|a|one|two|three|four|six) (?:days?|weeks?|months?)\b",
        r"\ba\.?s\.?a\.?p\.?(?![a-z])",
        r"\bimmediately\b",
        r"\bright away\b",
        r"\bq[1-4]\b(?:\s*\d{4})?",
        r"\b(?:end of|early|mid|late)\s+"
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
        r"\b(?:january|february|march|april|june|july|august|september|october|november"
        r"|december)\b",
    ]
]
_GREETING_ONLY_RE = re.compile(
    r"^(hi|hello|hey|yo|hiya|good\s+(morning|afternoon|evening)|namaste)\W*$", re.IGNORECASE
)
_NEED_PREFIX_RE = re.compile(
    r"^(?:(?:we|i)(?:'re|'m| are| am)?\s+)?(?:are\s+|am\s+)?"
    r"(?:really\s+)?(?:interested in|looking for|looking to|need|want|would like)\s+"
    r"(?:to get\s+|to\s+)?(?:a\s+|an\s+|some\s+)?",
    re.IGNORECASE,
)
_AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|sounds good|let'?s do it|book|schedule|call|meet|meeting|"
    r"available|slot|works for me)\b",
    re.IGNORECASE,
)
_RESUBSCRIBE = {"start", "unstop", "subscribe"}

INTENT_KEYWORDS: dict[str, list[str]] = {
    "Unqualified": [
        "not interested", "no thanks", "no thank you", "stop", "unsubscribe", "remove me",
        "wrong number", "do not contact", "don't contact", "dont contact", "leave me alone",
    ],
    "Hot": [
        "send proposal", "proposal", "demo", "pricing", "price quote", "quote", "interested",
        "sign up", "signup", "let's do it", "lets do it", "ready to buy", "buy", "purchase",
        "yes please", "book a call", "schedule a call", "contract",
    ],
    "Warm": [
        "maybe", "later", "send info", "send me info", "more info", "more information",
        "thinking about", "considering", "could be", "possibly", "not yet", "next quarter",
    ],
    "Cold": [
        "not sure", "just looking", "browsing", "just checking", "curious", "window shopping",
        "no budget",
    ],
}
_INTENT_PRIORITY = ["Unqualified", "Hot", "Warm", "Cold"]
OPT_OUT_PHRASES = INTENT_KEYWORDS["Unqualified"]


def _phrase_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])")


_ALL_PHRASES = sorted(
    ((label, kw, _phrase_re(kw)) for label, kws in INTENT_KEYWORDS.items() for kw in kws),
    key=lambda item: -len(item[1]),
)
_OPT_OUT_RES = [_phrase_re(p) for p in OPT_OUT_PHRASES]


@dataclass
class ConversationState:
    name: str = ""
    stage: str = "New"
    slots: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    reply: str
    slots: dict[str, Any] = field(default_factory=dict)
    agent_stage: str = "greet"
    kanban_stage: str | None = None
    intent: tuple[str, float, list[str]] | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    opted_out: bool = False
    resubscribed: bool = False
    requested_time: datetime | None = None
    offer_slots: bool = False
    used_llm: bool = False


# ------------------------------------------------------------ extraction ---

def extract_budget(text: str) -> str | None:
    """Pull a budget amount out of free text as a display string."""
    m = _BUDGET_RE.search(text)
    if m:
        raw = (m.group(1) or m.group(3) or "").replace(",", "")
        suffix = (m.group(2) or m.group(4) or "").lower()
        try:
            amount = float(raw) * _SCALE.get(suffix, 1.0)
        except ValueError:
            return None
        prefix = m.group(0).lower()
        if "\u20b9" in prefix or "rs" in prefix or "inr" in prefix or suffix in _INR_SUFFIXES:
            cur = "\u20b9"
        elif "\u20b1" in prefix or "php" in prefix:
            cur = "\u20b1"
        else:
            cur = "$"
        return f"{cur}{amount:,.0f}"
    if "budget" in text.lower():
        n = _NUMBER_RE.search(text)
        if n:
            return f"${float(n.group(0).replace(',', '')):,.0f}"
    return None


def extract_timeline(text: str) -> str | None:
    for rx in _TIMELINE_RES:
        m = rx.search(text)
        if m:
            return m.group(0).strip()
    return None


def extract_need(text: str) -> str | None:
    """The lead's need: first sentence, greeting and 'we are interested in' stripped."""
    cleaned = text.strip()
    if not cleaned or _GREETING_ONLY_RE.match(cleaned):
        return None
    first = re.split(r"[.!?;\n]", cleaned, maxsplit=1)[0].strip()
    first = re.sub(r"^(hi|hello|hey|yo|hiya)\b[,!.]?\s*", "", first, flags=re.IGNORECASE)
    first = _NEED_PREFIX_RE.sub("", first)
    first = first[:120].strip(" ,")
    return first or None


def is_opt_out(text: str) -> bool:
    lowered = text.lower()
    return any(rx.search(lowered) for rx in _OPT_OUT_RES)


def classify_intent(messages: list[str]) -> tuple[str, float, list[str]]:
    """Keyword-score the conversation into Hot / Warm / Cold / Unqualified.

    Longer phrases win and are masked, so "not interested" is never also
    counted as "interested".
    """
    blob = " ".join(messages).lower()
    hits: dict[str, list[str]] = {label: [] for label in _INTENT_PRIORITY}
    for label, kw, rx in _ALL_PHRASES:
        if rx.search(blob):
            hits[label].append(kw)
            blob = rx.sub(" ", blob)
    total = sum(len(v) for v in hits.values())
    if total == 0:
        return "Cold", 0.4, ["no buying signals detected yet"]
    label = max(_INTENT_PRIORITY,
                key=lambda lab: (len(hits[lab]), -_INTENT_PRIORITY.index(lab)))
    reasons = [f"mentioned '{kw}'" for kw in hits[label][:4]]
    return label, round(len(hits[label]) / total, 2), reasons


# ---------------------------------------------------------------- replies ---

def _first_name(name: str) -> str:
    parts = name.split()
    return f" {parts[0]}" if parts else ""


def compose_reply(slots: dict[str, Any], target: str, lead_name: str) -> str:
    need, budget, timeline = slots.get("need"), slots.get("budget"), slots.get("timeline")
    name = _first_name(lead_name)
    if target == "needs":
        return (f"Hi{name}! Thanks for reaching out. What are you hoping to solve or "
                "improve? A sentence or two is plenty.")
    if target == "budget":
        ref = f" for {need}" if need else ""
        return (f"Thanks{name}, that helps. Do you have a rough budget range in mind{ref}? "
                "Even a ballpark works.")
    if target == "timeline":
        ref = f" with a budget around {budget}" if budget else ""
        return (f"Noted{name}{ref}. When are you hoping to have this in place? "
                "Something like 'next month' or 'Q3' is fine.")
    if target == "intent":
        return (f"Perfect{name}. Last quick one: how are you handling this today, "
                "and what would make you switch?")
    if target == "book":
        bits = [f"{k}: {v}" for k, v in (("need", need), ("budget", budget),
                                           ("timeline", timeline)) if v]
        summary = f" ({'; '.join(bits)})" if bits else ""
        return (f"Thanks{name}! Based on what you shared{summary}, it looks like a strong fit. "
                "Want me to book a short intro call? Reply with a day and time that works.")
    return (f"No problem{name}, I will step back and you will not get further messages. "
            "If anything changes, just reply here and a human will pick it up.")


def compose_slot_offer(lead_name: str, slot_labels: list[str]) -> str:
    name = _first_name(lead_name)
    if not slot_labels:
        return (f"Thanks{name}! I could not find an open slot in the next couple of weeks; "
                "a teammate will reach out to schedule personally.")
    return (f"Great{name}! Here are the next open times for a short intro call: "
            f"{'; '.join(slot_labels)}. Reply with the one that suits you "
            "(or another day and time).")


def compose_booking_confirmation(lead_name: str, when_label: str) -> str:
    return (f"You're booked{_first_name(lead_name)}: intro call on {when_label}. "
            "A calendar invite is on its way. Reply here if you need to change it.")


def compose_unavailable(lead_name: str, requested_label: str, slot_labels: list[str]) -> str:
    alt = f" Open times nearby: {'; '.join(slot_labels)}." if slot_labels else ""
    return (f"Sorry{_first_name(lead_name)}, {requested_label} is not available.{alt} "
            "Which one works for you?")


# ------------------------------------------------------------------ flow ---

def next_target(slots: dict[str, Any], agent_stage: str) -> str:
    order = ["needs", "budget", "timeline", "intent", "book"]
    slot_for = {"needs": "need", "budget": "budget", "timeline": "timeline"}
    start = order.index(agent_stage) if agent_stage in order else 0
    for target in order[start:]:
        if target in slot_for and slots.get(slot_for[target]):
            continue
        if target == "intent" and slots.get("intent_asked"):
            continue
        return target
    return "book"


LLM_SYSTEM_PROMPT = (
    "You are a concise, friendly B2B lead-qualification assistant chatting over SMS/WhatsApp. "
    "You receive JSON with the conversation stage, known slots, recent lead messages, the "
    "latest message and a draft reply. Rewrite the draft so it reads naturally and responds "
    "to the latest message, keeping its question and every fact in it; never invent prices, "
    "dates or promises. Also judge the lead's buying intent. Respond with JSON only: "
    '{"reply": str, "intent": "Hot"|"Warm"|"Cold"|"Unqualified", "confidence": 0..1, '
    '"reason": str, "slots": {"need": str|null, "budget": str|null, "timeline": str|null}}. '
    "Keep the reply under 320 characters, no emojis."
)


def apply_llm(llm: LLMClient, result: AgentResult, target: str, text: str,
              history: list[str]) -> None:
    """Let the LLM polish the reply and refine intent/slots. Never raises."""
    rule_label, rule_conf, rule_reasons = result.intent or ("Cold", 0.4, [])
    context = {
        "stage": target,
        "slots": {k: result.slots.get(k) for k in ("need", "budget", "timeline")},
        "recent_messages": history[-6:],
        "latest_message": text,
        "draft_reply": result.reply,
        "rule_intent": {"label": rule_label, "confidence": rule_conf},
    }
    data = llm.complete_json(LLM_SYSTEM_PROMPT, json.dumps(context))
    if not data:
        return
    reply = data.get("reply")
    if isinstance(reply, str) and reply.strip():
        result.reply = reply.strip()[:MAX_REPLY_CHARS]
        result.used_llm = True
    label = data.get("intent")
    if label in INTENTS and label != "Unqualified":  # opt-out stays rule-based
        try:
            conf = min(1.0, max(0.0, float(data.get("confidence", rule_conf))))
        except (TypeError, ValueError):
            conf = rule_conf
        reason = str(data.get("reason") or "").strip()[:200]
        reasons = ([f"llm: {reason}"] if reason else []) + rule_reasons
        result.intent = (str(label), round(conf, 2), reasons[:4])
        result.used_llm = True
    slots = data.get("slots")
    if isinstance(slots, dict):
        for key in ("need", "budget", "timeline"):
            value = slots.get(key)
            if isinstance(value, str) and value.strip() and not result.slots.get(key):
                result.slots[key] = value.strip()[:120]
                result.events.append(
                    ("slot_filled", {"slot": key, "value": result.slots[key], "by": "llm"})
                )


def process_inbound(
    state: ConversationState,
    text: str,
    history: list[str],
    now_local: datetime,
    llm: LLMClient | None = None,
) -> AgentResult:
    """Handle one inbound lead message; returns the reply and state changes."""
    slots = dict(state.slots)
    previous_stage = str(slots.get("_agent_stage", "greet"))
    agent_stage = previous_stage
    result = AgentResult(reply="", agent_stage=agent_stage, slots=slots)

    if is_opt_out(text):
        slots["_agent_stage"] = "handoff"
        result.agent_stage = "handoff"
        result.opted_out = True
        result.intent = ("Unqualified", 0.95, ["explicit opt-out language"])
        result.reply = compose_reply(slots, "handoff", state.name)
        result.events += [("intent_classified", {"intent": "Unqualified"}),
                          ("opted_out", {}),
                          ("stage_changed", {"from": previous_stage, "to": "handoff"})]
        return result

    if text.strip().lower() in _RESUBSCRIBE:
        result.resubscribed = True
        result.events.append(("resubscribed", {}))
    if agent_stage == "handoff":
        # A lead who opted out and writes again re-opens the conversation.
        agent_stage = "needs" if not slots.get("need") else "budget"

    if agent_stage in ("greet", "needs") and not slots.get("need") and not result.resubscribed:
        need = extract_need(text)
        if need:
            slots["need"] = need
            result.events.append(("slot_filled", {"slot": "need", "value": need}))
    for key, extractor in (("budget", extract_budget), ("timeline", extract_timeline)):
        value = extractor(text)
        if value and not slots.get(key):
            slots[key] = value
            result.events.append(("slot_filled", {"slot": key, "value": value}))

    requested = parse_requested_time(text, now_local)
    target = next_target(slots, agent_stage)
    if requested is not None and target in ("intent", "book"):
        target = "book"
    if target != previous_stage:
        result.events.append(("stage_changed", {"from": previous_stage, "to": target}))
    slots["_agent_stage"] = target
    result.agent_stage = target
    if target == "intent":
        slots["intent_asked"] = True

    result.intent = classify_intent([*history, text])
    result.reply = compose_reply(slots, target, state.name)

    if target == "book":
        result.requested_time = requested
        result.offer_slots = requested is None and (
            previous_stage == "book" or bool(_AFFIRM_RE.search(text))
        )

    if llm is not None and result.requested_time is None and not result.offer_slots:
        apply_llm(llm, result, target, text, history)

    label = result.intent[0]
    result.events.append(("intent_classified", {"intent": label, "confidence": result.intent[1]}))
    if state.stage == "New":
        result.kanban_stage = "Qualified" if label == "Hot" else "Contacted"
    elif label == "Hot" and state.stage == "Contacted":
        result.kanban_stage = "Qualified"
    return result
