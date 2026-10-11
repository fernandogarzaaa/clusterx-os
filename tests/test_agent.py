from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from clusterx_os import agent
from clusterx_os.agent import (
    ConversationState,
    classify_intent,
    extract_budget,
    extract_need,
    extract_timeline,
    is_opt_out,
    next_target,
    process_inbound,
)
from clusterx_os.integrations.llm import MockLLM

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


class FakeLLM:
    name = "fake"

    def __init__(self, response: dict[str, Any] | None) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        self.calls.append((system, json.loads(user)))
        return self.response

    def status(self) -> dict[str, Any]:
        return {"name": self.name}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Our budget is $5000", "$5,000"),
        ("about $5,000.50", "$5,000"),
        ("$8k", "$8,000"),
        ("USD 12000", "$12,000"),
        ("usd 2.5 million", "$2,500,000"),
        ("2.5 lakh", "\u20b9250,000"),
        ("1 crore", "\u20b910,000,000"),
        ("2 cr", "\u20b920,000,000"),
        ("Rs. 50000", "\u20b950,000"),
        ("INR 75000", "\u20b975,000"),
        ("\u20b9 9000", "\u20b99,000"),
        ("PHP 150000", "\u20b1150,000"),
        ("\u20b1 2k", "\u20b12,000"),
        ("10 thousand", "$10,000"),
        ("3m", "$3,000,000"),
        ("1.5mm", "$1,500,000"),
        ("budget is around 7000 or so", "$7,000"),
        ("Budget: 1,200", "$1,200"),
    ],
)
def test_extract_budget(text: str, expected: str) -> None:
    assert extract_budget(text) == expected


@pytest.mark.parametrize("text", ["we have 40 people", "no idea yet", "budget is flexible", ""])
def test_extract_budget_none(text: str) -> None:
    assert extract_budget(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We want this next month", "next month"),
        ("ideally this quarter", "this quarter"),
        ("in 2 weeks", "in 2 weeks"),
        ("in a month", "in a month"),
        ("ASAP please", "ASAP"),
        ("a.s.a.p.", "a.s.a.p."),
        ("immediately", "immediately"),
        ("right away", "right away"),
        ("Q3 2027", "Q3 2027"),
        ("by q1", "q1"),
        ("end of march", "end of march"),
        ("early sept", "early sept"),
        ("in november", "november"),
    ],
)
def test_extract_timeline(text: str, expected: str) -> None:
    assert extract_timeline(text) == expected


@pytest.mark.parametrize("text", ["maybe later", "we may start soon", "asapx", ""])
def test_extract_timeline_none(text: str) -> None:
    assert extract_timeline(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hi, we are interested in a demo of your product. Thanks!", "demo of your product"),
        ("We need faster follow up", "faster follow up"),
        ("I'm looking for a CRM for my team", "CRM for my team"),
        ("Hello! We would like to automate lead intake", "automate lead intake"),
        ("Our sales team drops leads; help", "Our sales team drops leads"),
        ("hey, need some help with scheduling", "help with scheduling"),
    ],
)
def test_extract_need(text: str, expected: str) -> None:
    assert extract_need(text) == expected


@pytest.mark.parametrize("text", ["hi", "Hello!", "good morning", "  ", "hey."])
def test_extract_need_greeting_only(text: str) -> None:
    assert extract_need(text) is None


def test_extract_need_truncates() -> None:
    need = extract_need("x" * 300)
    assert need is not None and len(need) == 120


@pytest.mark.parametrize(
    "text",
    ["STOP", "please stop texting", "Unsubscribe", "not interested", "wrong number",
     "Don't contact me again", "remove me from this list", "No thanks"],
)
def test_opt_out_detected(text: str) -> None:
    assert is_opt_out(text)


@pytest.mark.parametrize("text", ["nonstop growth", "stopwatch app", "we are interested", ""])
def test_opt_out_word_boundaries(text: str) -> None:
    assert not is_opt_out(text)


def test_classify_hot() -> None:
    label, conf, reasons = classify_intent(["please send the proposal and pricing"])
    assert label == "Hot"
    assert conf == 1.0
    assert reasons == ["mentioned 'proposal'", "mentioned 'pricing'"]


def test_classify_warm_and_cold() -> None:
    assert classify_intent(["maybe later, send me info"])[0] == "Warm"
    assert classify_intent(["just looking, not sure"])[0] == "Cold"


def test_classify_no_signal() -> None:
    assert classify_intent(["hello there"]) == ("Cold", 0.4, ["no buying signals detected yet"])


def test_classify_masks_negation() -> None:
    label, conf, reasons = classify_intent(["I am not interested"])
    assert label == "Unqualified"
    assert conf == 1.0
    assert reasons == ["mentioned 'not interested'"]


def test_classify_tie_uses_priority() -> None:
    # one Hot ("demo") and one Warm ("maybe") hit: Hot outranks Warm on a tie
    label, conf, _ = classify_intent(["maybe a demo"])
    assert label == "Hot"
    assert conf == 0.5


def test_classify_majority_wins() -> None:
    label, conf, _ = classify_intent(["maybe later, possibly, demo"])
    assert label == "Warm"
    assert conf == 0.75


def test_classify_reasons_capped_at_four() -> None:
    _, _, reasons = classify_intent(["demo pricing proposal contract purchase quote"])
    assert len(reasons) == 4


@pytest.mark.parametrize(
    ("slots", "stage", "expected"),
    [
        ({}, "greet", "needs"),
        ({"need": "x"}, "greet", "budget"),
        ({"need": "x", "budget": "$1"}, "needs", "timeline"),
        ({"need": "x", "budget": "$1", "timeline": "q1"}, "budget", "intent"),
        ({"need": "x", "budget": "$1", "timeline": "q1", "intent_asked": True}, "intent", "book"),
        ({"need": "x"}, "timeline", "timeline"),
        ({}, "book", "book"),
        ({}, "unknown-stage", "needs"),
    ],
)
def test_next_target(slots: dict[str, Any], stage: str, expected: str) -> None:
    assert next_target(slots, stage) == expected


def _run(texts: list[str], name: str = "Dana Scully", stage: str = "New",
         llm: Any = None) -> tuple[list[agent.AgentResult], dict[str, Any]]:
    slots: dict[str, Any] = {}
    history: list[str] = []
    results = []
    for text in texts:
        result = process_inbound(ConversationState(name, stage, slots), text, history, NOW, llm)
        slots = result.slots
        history.append(text)
        if result.kanban_stage:
            stage = result.kanban_stage
        results.append(result)
    return results, slots


def test_full_conversation_walks_stages() -> None:
    results, slots = _run([
        "Hi, we need faster lead follow-up",
        "Budget is about $5000",
        "We want it live next month",
        "We use spreadsheets; we'd switch for automation",
        "yes, book a call",
    ])
    assert [r.agent_stage for r in results] == ["budget", "timeline", "intent", "book", "book"]
    assert slots["need"] == "faster lead follow-up"
    assert slots["budget"] == "$5,000"
    assert slots["timeline"] == "next month"
    assert slots["intent_asked"] is True
    assert results[3].offer_slots is False
    assert results[4].offer_slots is True
    assert results[4].requested_time is None
    assert len({r.reply for r in results[:4]}) == 4
    assert results[0].reply.startswith("Thanks Dana, that helps.")
    assert "faster lead follow-up" in results[0].reply


def test_book_stage_parses_requested_time() -> None:
    results, _ = _run(["We need a CRM", "$10k", "asap", "we use excel", "tomorrow at 3pm"])
    assert results[-1].agent_stage == "book"
    assert results[-1].requested_time == datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
    assert results[-1].offer_slots is False


def test_requested_time_jumps_from_intent_to_book() -> None:
    results, _ = _run(["We need a CRM", "$10k by Q1", "Friday 10am?"])
    assert results[1].agent_stage == "intent"
    assert results[2].agent_stage == "book"
    assert results[2].requested_time == datetime(2026, 10, 16, 10, 0, tzinfo=UTC)


def test_time_in_early_stage_is_not_a_booking() -> None:
    results, _ = _run(["We need help, call me at 3pm"])
    assert results[0].agent_stage == "budget"
    assert results[0].requested_time is None


def test_book_stage_offer_on_affirmation_only() -> None:
    results, _ = _run(["We need a CRM", "$10k", "asap", "we use excel"])
    # reaching book for the first time without a yes: summary only, no slot offer
    assert results[-1].agent_stage == "book"
    assert results[-1].offer_slots is False
    assert "strong fit" in results[-1].reply


def test_opt_out_short_circuits() -> None:
    results, slots = _run(["Not interested, stop messaging me"])
    r = results[0]
    assert r.opted_out is True
    assert r.agent_stage == "handoff"
    assert slots["_agent_stage"] == "handoff"
    assert r.intent == ("Unqualified", 0.95, ["explicit opt-out language"])
    assert r.kanban_stage is None
    assert ("opted_out", {}) in r.events
    assert "will not get further messages" in r.reply


def test_message_after_opt_out_reopens() -> None:
    results, slots = _run(["stop", "Actually we need a chatbot"])
    assert results[1].opted_out is False
    assert results[1].agent_stage == "budget"
    assert slots["need"] == "chatbot"


def test_resubscribe_keyword() -> None:
    results, slots = _run(["stop", "START"])
    assert results[1].resubscribed is True
    assert ("resubscribed", {}) in results[1].events
    assert "need" not in slots
    assert results[1].agent_stage == "needs"


def test_kanban_progression() -> None:
    first = process_inbound(ConversationState("A", "New", {}), "hello", [], NOW)
    assert first.kanban_stage == "Contacted"
    hot_new = process_inbound(ConversationState("A", "New", {}), "we want a demo", [], NOW)
    assert hot_new.kanban_stage == "Qualified"
    hot_contacted = process_inbound(ConversationState("A", "Contacted", {}), "pricing?", [], NOW)
    assert hot_contacted.kanban_stage == "Qualified"
    warm_contacted = process_inbound(ConversationState("A", "Contacted", {}), "maybe", [], NOW)
    assert warm_contacted.kanban_stage is None
    hot_booked = process_inbound(ConversationState("A", "Booked", {}), "pricing?", [], NOW)
    assert hot_booked.kanban_stage is None


def test_events_record_slots_and_stage() -> None:
    r = process_inbound(ConversationState("A", "New", {}), "We need X with $2k by Q2", [], NOW)
    kinds = [k for k, _ in r.events]
    assert kinds.count("slot_filled") == 3
    assert ("stage_changed", {"from": "greet", "to": "intent"}) in r.events
    assert kinds[-1] == "intent_classified"


def test_slots_not_overwritten() -> None:
    r = process_inbound(ConversationState("A", "Contacted",
                                          {"need": "n", "budget": "$1", "_agent_stage": "budget"}),
                        "actually $9000", [], NOW)
    assert r.slots["budget"] == "$1"


def test_compose_reply_variants() -> None:
    assert agent.compose_reply({}, "needs", "").startswith("Hi! Thanks")
    assert "budget around $5" in agent.compose_reply({"budget": "$5"}, "timeline", "Jo Doe")
    assert agent.compose_reply({}, "intent", "Jo").startswith("Perfect Jo.")
    book = agent.compose_reply({"need": "n", "budget": "$1", "timeline": "q1"}, "book", "Jo")
    assert "(need: n; budget: $1; timeline: q1)" in book
    assert "shared, it looks" in agent.compose_reply({}, "book", "")


def test_slot_offer_and_confirmation_text() -> None:
    assert "a teammate will reach out" in agent.compose_slot_offer("Jo", [])
    offer = agent.compose_slot_offer("Jo Doe", ["Mon 9 AM", "Mon 10 AM"])
    assert offer.startswith("Great Jo!") and "Mon 9 AM; Mon 10 AM" in offer
    assert agent.compose_booking_confirmation("Jo", "Tue 1 PM") .startswith(
        "You're booked Jo: intro call on Tue 1 PM.")
    assert "Open times nearby: A; B." in agent.compose_unavailable("Jo", "X", ["A", "B"])
    assert "Open times" not in agent.compose_unavailable("Jo", "X", [])


# ------------------------------------------------------------------ LLM ---

def test_llm_rewrites_reply_and_intent() -> None:
    llm = FakeLLM({"reply": " Lovely, what's the budget? ", "intent": "Warm", "confidence": 0.8,
                   "reason": "sounds keen", "slots": {"timeline": "next spring"}})
    r = process_inbound(ConversationState("A", "New", {}), "we need a CRM", [], NOW, llm)
    assert r.reply == "Lovely, what's the budget?"
    assert r.intent == ("Warm", 0.8, ["llm: sounds keen", "no buying signals detected yet"])
    assert r.slots["timeline"] == "next spring"
    assert ("slot_filled", {"slot": "timeline", "value": "next spring", "by": "llm"}) in r.events
    assert r.used_llm is True
    system, ctx = llm.calls[0]
    assert system == agent.LLM_SYSTEM_PROMPT
    assert ctx["stage"] == "budget"
    assert ctx["latest_message"] == "we need a CRM"
    assert ctx["rule_intent"] == {"label": "Cold", "confidence": 0.4}
    assert ctx["draft_reply"].startswith("Thanks A")


def test_llm_cannot_mark_unqualified_or_overwrite_slots() -> None:
    llm = FakeLLM({"intent": "Unqualified", "slots": {"need": "other"}})
    r = process_inbound(ConversationState("A", "New", {}), "we need a CRM", [], NOW, llm)
    assert r.intent is not None and r.intent[0] == "Cold"
    assert r.slots["need"] == "CRM"
    assert r.used_llm is False


def test_llm_bad_values_are_ignored() -> None:
    llm = FakeLLM({"reply": "  ", "intent": "Spicy", "confidence": "high", "slots": "nope"})
    r = process_inbound(ConversationState("A", "New", {}), "we need a CRM", [], NOW, llm)
    assert r.reply.startswith("Thanks A")
    assert r.used_llm is False


def test_llm_confidence_clamped_and_defaulted() -> None:
    r = process_inbound(ConversationState("A", "New", {}), "demo", [], NOW,
                        FakeLLM({"intent": "Hot", "confidence": 7}))
    assert r.intent == ("Hot", 1.0, ["mentioned 'demo'"])
    r = process_inbound(ConversationState("A", "New", {}), "demo", [], NOW,
                        FakeLLM({"intent": "Hot", "confidence": "x"}))
    assert r.intent == ("Hot", 1.0, ["mentioned 'demo'"])
    r = process_inbound(ConversationState("A", "New", {}), "demo", [], NOW,
                        FakeLLM({"intent": "Warm", "confidence": -2}))
    assert r.intent == ("Warm", 0.0, ["mentioned 'demo'"])


def test_llm_reply_truncated() -> None:
    r = process_inbound(ConversationState("A", "New", {}), "hello", [], NOW,
                        FakeLLM({"reply": "y" * 5000}))
    assert len(r.reply) == agent.MAX_REPLY_CHARS


def test_llm_failure_keeps_rule_result() -> None:
    r = process_inbound(ConversationState("A", "New", {}), "hello", [], NOW, FakeLLM(None))
    assert r.reply.startswith("Hi A!")
    assert r.used_llm is False


def test_llm_not_used_for_bookings_or_opt_out() -> None:
    llm = FakeLLM({"reply": "LLM"})
    slots = {"need": "n", "budget": "$1", "timeline": "q1", "intent_asked": True,
             "_agent_stage": "book"}
    r = process_inbound(ConversationState("A", "Qualified", slots), "monday 10am", [], NOW, llm)
    assert r.requested_time is not None
    r2 = process_inbound(ConversationState("A", "Qualified", slots), "sure", [], NOW, llm)
    assert r2.offer_slots is True
    r3 = process_inbound(ConversationState("A", "Qualified", {}), "stop", [], NOW, llm)
    assert r3.opted_out
    assert llm.calls == []


def test_llm_history_window_is_last_six() -> None:
    llm = FakeLLM(None)
    history = [f"m{i}" for i in range(10)]
    process_inbound(ConversationState("A", "Contacted", {}), "hello", history, NOW, llm)
    assert llm.calls[0][1]["recent_messages"] == history[-6:]


def test_mock_llm_round_trip() -> None:
    r = process_inbound(ConversationState("A", "New", {}), "demo please", [], NOW, MockLLM())
    assert r.used_llm is True
    assert r.intent is not None and r.intent[0] == "Hot"
    assert r.intent[2][0] == "llm: mock llm agreed with rule engine"


# ------------------------------------------------- mutation-test hardening ---

def test_budget_lakh_and_lakhs_scale_identically() -> None:
    assert extract_budget("2 lakh") == extract_budget("2 lakhs") == "₹200,000"


def test_budget_symbol_without_digits_is_ignored() -> None:
    # The regex accepts a bare comma after a currency sign; that is not an amount.
    assert extract_budget("$,") is None
    assert extract_budget("$,k") is None  # matches the pattern, but "" is not a number


def test_intent_confidence_rounds_to_two_places() -> None:
    label, conf, reasons = classify_intent(["pricing please, and a demo", "maybe"])
    assert (label, conf) == ("Hot", 0.67)
    assert reasons == ["mentioned 'pricing'", "mentioned 'demo'"]


def _llm_result(response: dict[str, Any]) -> agent.AgentResult:
    result = agent.AgentResult(reply="draft", intent=("Warm", 0.5, ["r1", "r2", "r3", "r4"]))
    agent.apply_llm(FakeLLM(response), result, "needs", "hello", [])
    return result


def test_llm_reply_alone_marks_llm_used_and_is_capped() -> None:
    result = _llm_result({"reply": "x" * 1500})
    assert result.used_llm is True
    assert result.reply == "x" * 1000
    assert result.intent == ("Warm", 0.5, ["r1", "r2", "r3", "r4"])


def test_llm_intent_reason_and_confidence_are_bounded() -> None:
    result = _llm_result({"intent": "Hot", "confidence": 0.66666, "reason": "y" * 300})
    assert result.reply == "draft"
    assert result.intent == ("Hot", 0.67, ["llm: " + "y" * 200, "r1", "r2", "r3"])


def test_llm_slot_values_are_capped() -> None:
    result = _llm_result({"slots": {"need": "n" * 300}})
    assert result.slots["need"] == "n" * 120


def test_intent_event_carries_confidence() -> None:
    r = process_inbound(ConversationState("A", "New", {}), "we want a demo", [], NOW)
    assert r.intent is not None
    assert ("intent_classified", {"intent": r.intent[0], "confidence": r.intent[1]}) in r.events


def test_llm_intent_without_rule_intent_defaults_confidence() -> None:
    result = agent.AgentResult(reply="draft")
    agent.apply_llm(FakeLLM({"intent": "Warm"}), result, "needs", "hello", [])
    assert result.intent == ("Warm", 0.4, [])


_READY = {"need": "n", "budget": "$1", "timeline": "q1", "intent_asked": True}


def test_offer_slots_when_lead_agrees_on_entering_book() -> None:
    r = process_inbound(ConversationState("A", "Qualified", {**_READY, "_agent_stage": "intent"}),
                        "sure", [], NOW)
    assert r.agent_stage == "book" and r.offer_slots is True


def test_offer_slots_again_while_already_booking() -> None:
    r = process_inbound(ConversationState("A", "Qualified", {**_READY, "_agent_stage": "book"}),
                        "hmm", [], NOW)
    assert r.agent_stage == "book" and r.offer_slots is True
