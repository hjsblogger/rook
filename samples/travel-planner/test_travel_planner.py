"""Tests for travel-planner. No model key needed: the model is scripted.

The MCP servers are the real ones, spawned over stdio, so these tests cover
the protocol, the delegation, the parallel specialists, the verification, the
revision loop and the booking desk, everything except what a real model would
choose.

    python3 -m unittest -v test_travel_planner
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from travel_planner import agents, llm, mcp, planner
from travel_planner.__main__ import Session

REQUEST = "Two of us, New York to Lisbon, 12 to 18 December 2026, $4,000 total. We like food and fado."

BAD_PICKS = """STATUS: COMPLETE
TRAVELLERS: 2
BUDGET_USD: 4000
PREFERENCES: none
FLIGHTS: FL-JFK-LIS-20261212-0, FL-LIS-JFK-20261218-1
STAYS: ST-LIS-01 x1
ACTIVITIES: AC-LIS-01, AC-LIS-03

The cheapest fare out, a guesthouse in Alfama."""

GOOD_PICKS = BAD_PICKS.replace("FL-JFK-LIS-20261212-0", "FL-JFK-LIS-20261212-1")


def _text(text: str) -> dict:
    return {"text": text, "tool_calls": [], "raw_output": [{"role": "assistant", "content": text}],
            "stop_reason": "end", "usage": {"input_tokens": 10, "output_tokens": 5}}


def _calls(*calls: tuple[str, dict]) -> dict:
    tcs = [{"call_id": f"c{i}-{name}", "name": name, "arguments": args} for i, (name, args) in enumerate(calls)]
    raw = [{"type": "function_call", "call_id": t["call_id"], "name": t["name"], "arguments": json.dumps(t["arguments"])} for t in tcs]
    return {"text": "", "tool_calls": tcs, "raw_output": raw, "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}


SPECIALIST_SCRIPT = {
    "flights specialist": [("search_flights", {"origin": "JFK", "destination": "Lisbon", "depart_date": "2026-12-12", "return_date": "2026-12-18", "passengers": 2})],
    "accommodation specialist": [("search_stays", {"city": "Lisbon", "check_in": "2026-12-12", "check_out": "2026-12-18", "guests": 2}), ("get_reviews", {"stay_id": "ST-LIS-02"})],
    "destinations specialist": [("search_activities", {"city": "Lisbon"}), ("travel_advisories", {"city": "Lisbon", "start_date": "2026-12-12", "end_date": "2026-12-18"})],
    "weather specialist": [("forecast", {"city": "Lisbon", "start_date": "2026-12-12", "end_date": "2026-12-18"})],
}

SEARCH_ALL = (("ask_flights_agent", {"task": "fares JFK-LIS"}), ("ask_stays_agent", {"task": "stays Lisbon"}),
              ("ask_activities_agent", {"task": "Lisbon research"}), ("ask_weather_agent", {"task": "Lisbon weather"}))


def _last_tool_output(items: list[dict]) -> dict:
    for item in reversed(items):
        if item.get("type") == "function_call_output":
            return json.loads(item["output"])
    return {}


class ScriptedModel:
    """Plays every role, chosen by the system prompt it is handed.

    The planner's behaviour is keyed on the traveller's latest message.
    """

    def __init__(self, picks: list[str] | None = None, writer_fails: bool = False) -> None:
        self.picks = list(picks or [])
        self.writer_fails = writer_fails
        self.seen_tools: dict[str, list[str]] = {}
        self.hold_id: str | None = None
        self.booking_id: str | None = None

    def __call__(self, items, tools=None, max_output_tokens=1200):
        system = items[0]["content"]
        if system.startswith("You write the day-by-day plan"):
            if self.writer_fails:
                raise llm.ModelError("could not reach the model: The read operation timed out")
            return _text("### Day 1 — Sat 2026-12-12 · Lisbon\nArrive and rest.")
        for key, script in SPECIALIST_SCRIPT.items():
            if system.startswith(f"You are the {key}"):
                self.seen_tools[key] = [t["name"] for t in tools or []]
                done = any(i.get("type") == "function_call_output" for i in items)
                return _text(f"{key} report") if done else _calls(*script)
        return self._planner(items)

    def _planner(self, items):
        users = [i for i in items if i.get("role") == "user"]
        goal = str(users[-1]["content"]) if users else ""
        fresh = items[-1].get("role") == "user"
        last = _last_tool_output(items)
        if goal.startswith("NEEDS ROOM"):
            return _text("STATUS: NEEDS_INPUT\nWhat is your total budget?")
        if goal == "hold and confirm":
            if fresh:
                return _calls(("hold_trip", {}))
            if "hold_id" in last and "status" in last:
                self.hold_id = last["hold_id"]
                return _calls(("confirm_booking", {"hold_id": self.hold_id}))
            return _text(f"STATUS: ANSWER\nHeld {self.hold_id}. Shall I confirm?")
        if goal == "yes, confirm":
            if fresh:
                return _calls(("confirm_booking", {"hold_id": self.hold_id}))
            self.booking_id = last.get("booking_id")
            return _text(f"STATUS: ANSWER\nBooked: {self.booking_id}.")
        if goal == "cancel it":
            if fresh:
                return _calls(("quote_cancellation", {"booking_id": self.booking_id}), ("cancel_booking", {"booking_id": self.booking_id}))
            return _text("STATUS: ANSWER\nYou would get some back. Shall I cancel?")
        if goal == "yes, cancel":
            if fresh:
                return _calls(("cancel_booking", {"booking_id": self.booking_id}))
            return _text(f"STATUS: ANSWER\n{'Cancelled' if last.get('cancelled') else 'Not cancelled'}.")
        if fresh and not goal.startswith("Your selection failed"):
            return _calls(*SEARCH_ALL)
        return _text(self.picks.pop(0))


class _TempState(unittest.TestCase):
    """Sessions and the bookings ledger in a temporary directory; weather pinned to a date."""

    def setUp(self) -> None:
        mcp.close_all()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BOOKINGS_FILE": str(Path(self.tmp.name) / "bookings.json"), "TRAVEL_TODAY": "2026-12-05"})
        self.env.start()

    def tearDown(self) -> None:
        mcp.close_all()
        self.env.stop()
        self.tmp.cleanup()

    def session(self, sid: str = "test") -> Session:
        s = Session(sid)
        s.dir = Path(self.tmp.name) / sid
        s.log_path = s.dir / "session.jsonl"
        s.artifacts_dir = s.dir / "artifacts"
        return s


class McpServers(_TempState):
    def test_each_server_lists_its_tools(self) -> None:
        expected = {
            "flights": ["list_airports", "search_flights", "get_fare"],
            "stays": ["search_stays", "get_stay", "get_reviews"],
            "destinations": ["destination_info", "search_activities", "travel_advisories"],
            "weather": ["forecast", "climate"],
            "bookings": ["create_hold", "confirm_hold", "quote_cancellation", "cancel_booking", "get_booking", "list_bookings"],
        }
        for name, tools in expected.items():
            self.assertEqual([t["name"] for t in mcp.server(name).tools()], tools, name)

    def test_a_fare_id_decodes_to_the_fare_search_returned(self) -> None:
        found = mcp.server("flights").call("search_flights", {"origin": "LHR", "destination": "KEF", "depart_date": "2026-02-10"})
        fare = found["outbound"][1]
        again = mcp.server("flights").call("get_fare", {"fare_id": fare["fare_id"]})
        self.assertEqual(again["price_usd_per_passenger"], fare["price_usd_per_passenger"])
        self.assertIn("rules", again)

    def test_flexible_dates_and_filters_report_what_they_removed(self) -> None:
        out = mcp.server("flights").call("search_flights", {"origin": "SFO", "destination": "KIX", "depart_date": "2027-04-02", "flex_days": 1, "nonstop_only": True})
        self.assertEqual({f["days_from_requested"] for f in out["outbound"]}, {-1, 0, 1})
        self.assertTrue(all(f["stops"] == 0 for f in out["outbound"]))
        self.assertEqual(out["filtered_out"], {"not nonstop": 3})

    def test_bad_input_is_a_tool_error_not_a_crash(self) -> None:
        out = mcp.server("stays").call("search_stays", {"city": "Lisbon", "check_in": "2026-12-18", "check_out": "2026-12-12"})
        self.assertIn("error", out)
        self.assertIn("error", mcp.server("flights").call("search_flights", {"origin": "JFK", "destination": "LIS", "depart_date": "2026-12-12", "flex_days": 9}))

    def test_advisories_are_filtered_by_date(self) -> None:
        during = mcp.server("destinations").call("travel_advisories", {"city": "Cape Town", "start_date": "2026-07-25", "end_date": "2026-07-30"})
        after = mcp.server("destinations").call("travel_advisories", {"city": "Cape Town", "start_date": "2026-12-01", "end_date": "2026-12-08"})
        self.assertEqual(len(during["advisories"]), 1)
        self.assertEqual(after["advisories"], [])

    def test_no_forecast_beyond_sixteen_days(self) -> None:
        out = mcp.server("weather").call("forecast", {"city": "Reykjavik", "start_date": "2026-12-15", "end_date": "2026-12-24"})
        self.assertEqual([d["date"] for d in out["days"]], [f"2026-12-{d}" for d in range(15, 21)])
        self.assertEqual(out["beyond_horizon"], [f"2026-12-{d}" for d in range(21, 25)])
        self.assertEqual(out["climate_normals"][0]["month"], "Dec")

    def test_booking_lifecycle_prices_from_records_and_refunds_by_terms(self) -> None:
        b = mcp.server("bookings")
        refused = b.call("create_hold", {"travellers": 2, "items": [{"kind": "fare", "id": "FL-JFK-LIS-20261212-0"}]})
        self.assertIn("1 seat(s) left", refused["error"])
        hold = b.call("create_hold", {"travellers": 2, "reference": "t", "items": [
            {"kind": "fare", "id": "FL-JFK-LIS-20261212-1"},
            {"kind": "stay", "id": "ST-LIS-01", "check_in": "2026-12-12", "check_out": "2026-12-18"},
        ]})
        fare = mcp.server("flights").call("get_fare", {"fare_id": "FL-JFK-LIS-20261212-1"})
        self.assertEqual(hold["total_usd"], 2 * fare["price_usd_per_passenger"] + 6 * 118)
        booking = b.call("confirm_hold", {"hold_id": hold["hold_id"]})
        self.assertTrue(booking["confirmed"])
        # Today is 2026-12-05: the stay is free to cancel until 7 days before (Dec 5), the fare never is.
        self.assertEqual(b.call("quote_cancellation", {"booking_id": booking["booking_id"]})["refund_usd"], 6 * 118)
        self.assertEqual(b.call("cancel_booking", {"booking_id": booking["booking_id"]})["status"], "cancelled")
        self.assertEqual(len(b.call("list_bookings", {"reference": "t"})["bookings"]), 1)

    def test_an_expired_hold_cannot_be_confirmed(self) -> None:
        mcp.close_all()
        with mock.patch.dict(os.environ, {"BOOKINGS_HOLD_MINUTES": "-1"}):
            b = mcp.server("bookings")
            hold = b.call("create_hold", {"travellers": 1, "items": [{"kind": "activity", "id": "AC-LIS-01"}]})
            out = b.call("confirm_hold", {"hold_id": hold["hold_id"]})
        self.assertFalse(out["confirmed"])
        self.assertIn("expired", out["reason"])


class Planner(_TempState):
    def test_full_turn_revises_an_unbookable_fare_and_prices_from_records(self) -> None:
        model = ScriptedModel([BAD_PICKS, GOOD_PICKS])
        session = self.session()
        with mock.patch.object(llm, "call", model):
            result = planner.run_turn(session, REQUEST)

        self.assertEqual(result["status"], "completed", result["answer"])
        self.assertEqual(result["metrics"]["revisions"], 1)
        self.assertEqual(result["picks"]["flights"][0], "FL-JFK-LIS-20261212-1")
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["writer"], "model")

        # Each specialist saw only its own server's tools.
        self.assertEqual(model.seen_tools["flights specialist"], ["list_airports", "search_flights", "get_fare"])
        self.assertEqual(model.seen_tools["weather specialist"], ["forecast", "climate"])
        self.assertNotIn("search_flights", model.seen_tools["accommodation specialist"])

        flights = mcp.server("flights")
        out = flights.call("get_fare", {"fare_id": "FL-JFK-LIS-20261212-1"})["price_usd_per_passenger"]
        back = flights.call("get_fare", {"fare_id": "FL-LIS-JFK-20261218-1"})["price_usd_per_passenger"]
        expected = 2 * out + 2 * back + 6 * 118 + 2 * 55 + 2 * 40
        self.assertEqual(result["total_usd"], expected)

        itinerary = Path(result["itinerary_file"]).read_text(encoding="utf-8")
        self.assertIn("### Day 1", itinerary)
        self.assertIn(f"**{expected:,}**", itinerary)
        trip = json.loads(Path(result["trip_file"]).read_text(encoding="utf-8"))
        self.assertEqual(trip["legs"][0]["nights"], 6)
        self.assertIn("Lisbon", trip["weather"])

        events = [e["event"] for e in session.entries()]
        self.assertEqual(events.count("subagent_start"), 5)  # four specialists + the writer

        # The trace the UI streams: every model call and every MCP call, with what it returned.
        entries = session.entries()
        model_agents = {e["agent"] for e in entries if e["event"] == "model_call"}
        self.assertEqual(model_agents, {"travel-planner", "flights-agent", "stays-agent", "activities-agent", "weather-agent", "itinerary-writer"})
        search = next(e for e in entries if e["event"] == "tool_call" and e["tool"] == "search_flights")
        self.assertEqual(search["server"], "flights")
        self.assertIn("FL-JFK-LIS-20261212-1", json.dumps(search["result"]))
        self.assertIsInstance(search["ms"], int)
        listed = [e for e in entries if e["event"] == "mcp_tools_listed"]
        self.assertEqual({e["server"] for e in listed}, {"flights", "stays", "destinations", "weather"})
        self.assertTrue(all(e.get("answer") for e in entries if e["event"] == "subagent_end" and e["agent"].endswith("-agent")))

    def test_a_plan_still_wrong_after_revision_is_reported_not_hidden(self) -> None:
        with mock.patch.object(llm, "call", ScriptedModel([BAD_PICKS, BAD_PICKS])):
            result = planner.run_turn(self.session(), REQUEST)
        self.assertTrue(any("1 seat(s) left for 2" in p for p in result["problems"]))
        self.assertIn("Unresolved problems", result["answer"])

    def test_missing_information_asks_instead_of_planning(self) -> None:
        with mock.patch.object(llm, "call", ScriptedModel()):
            result = planner.run_turn(self.session(), "NEEDS ROOM: a week in Kyoto")
        self.assertEqual(result["status"], "needs_input")
        self.assertIsNone(result["itinerary_file"])

    def test_a_writer_that_times_out_falls_back_to_a_planner_schedule(self) -> None:
        with mock.patch.object(llm, "call", ScriptedModel([GOOD_PICKS], writer_fails=True)):
            result = planner.run_turn(self.session(), REQUEST)
        self.assertEqual(result["writer"], "fallback")
        itinerary = Path(result["itinerary_file"]).read_text(encoding="utf-8")
        self.assertIn("could not be reached", itinerary)
        self.assertIn("Alfama fado evening", itinerary)
        # The fado evening is closed on Mondays; 2026-12-14 is a Monday.
        monday = itinerary.split("### Day 3 — Mon 2026-12-14")[1].split("### Day 4")[0]
        self.assertNotIn("fado", monday)

    def test_a_slow_writer_is_abandoned_at_its_deadline(self) -> None:
        import time as _time
        model = ScriptedModel([GOOD_PICKS])
        real = model.__call__

        def slow(items, tools=None, max_output_tokens=1200):
            if items[0]["content"].startswith("You write the day-by-day plan"):
                _time.sleep(5)
            return real(items, tools, max_output_tokens)

        started = _time.monotonic()
        with mock.patch.dict(os.environ, {"WRITER_TIMEOUT": "1"}), mock.patch.object(llm, "call", slow):
            result = planner.run_turn(self.session(), REQUEST)
        self.assertLess(_time.monotonic() - started, 4)
        self.assertEqual(result["writer"], "fallback")
        self.assertIn("longer than 1s", Path(result["itinerary_file"]).read_text(encoding="utf-8"))

    def test_money_moves_only_in_a_later_turn_than_it_was_shown(self) -> None:
        model = ScriptedModel([GOOD_PICKS])
        session = self.session("book")
        with mock.patch.object(llm, "call", model):
            planner.run_turn(session, REQUEST)
            held = planner.run_turn(session, "hold and confirm")
            self.assertEqual(held["status"], "answered")
            self.assertEqual(held["booking"]["hold"]["status"], "held")
            self.assertIsNone(held["booking"]["booking"])  # the same-turn confirm was refused

            booked = planner.run_turn(session, "yes, confirm")
            self.assertEqual(booked["booking"]["booking"]["status"], "confirmed")
            self.assertTrue(list(session.artifacts_dir.glob("booking-BKG-*.json")))

            asked = planner.run_turn(session, "cancel it")  # quote and cancel in one turn: cancel refused
            self.assertEqual(asked["booking"]["booking"]["status"], "confirmed")
            cancelled = planner.run_turn(session, "yes, cancel")
            self.assertEqual(cancelled["booking"]["booking"]["status"], "cancelled")

        refusals = [e for e in session.entries() if e["event"] == "subagent_end" and e.get("agent") == "booking-agent" and e.get("error")]
        self.assertEqual(len(refusals), 2)
        hold_call = next(e for e in session.entries() if e["event"] == "tool_call" and e["tool"] == "create_hold")
        self.assertEqual(hold_call["server"], "bookings")
        self.assertTrue(hold_call["result"]["hold_id"].startswith("HLD-"))

    def test_hold_refuses_a_plan_with_problems(self) -> None:
        session = self.session()
        state = {"plan": {"travellers": 2, "flights": [], "stays": [], "activities": [], "problems": ["over budget"]}}
        out = planner.booking_desk("hold_trip", {}, state, session, turn=2)
        self.assertIn("unresolved problems", out["error"])


def _seen_from_servers(*calls: tuple[str, str, dict]) -> dict:
    seen: dict = {}
    for server, tool, args in calls:
        result = mcp.server(server).call(tool, args)
        agents.remember(result, seen)
        if tool == "travel_advisories":
            seen.setdefault("advisory_checks", {})[result["city"]] = {}
    return seen


class Verification(_TempState):
    SEEN = {
        "fares": {
            "FL-LHR-KEF-20261010-1": {"fare_id": "FL-LHR-KEF-20261010-1", "origin": "LHR", "destination": "KEF", "depart_date": "2026-10-10", "depart_time": "08:35", "price_usd_per_passenger": 200, "seats_left": 9, "stops": 0, "refundable": False},
            "FL-KEF-LHR-20261014-1": {"fare_id": "FL-KEF-LHR-20261014-1", "origin": "KEF", "destination": "LHR", "depart_date": "2026-10-14", "depart_time": "12:35", "price_usd_per_passenger": 210, "seats_left": 9, "stops": 0, "refundable": False},
        },
        "stays": {"ST-KEF-03": {"stay_id": "ST-KEF-03", "name": "Grindavík Lava Cabins", "neighbourhood": "Reykjanes peninsula", "nightly_usd": 132, "max_guests": 4, "airport": "KEF", "city": "Reykjavik"}},
        "activities": {"AC-KEF-02": {"activity_id": "AC-KEF-02", "name": "Reykjanes lava field hike", "area": "Reykjanes peninsula", "price_usd": 95, "airport": "KEF", "city": "Reykjavik"}},
        "advisories": {"ADV-KEF-01": {"advisory_id": "ADV-KEF-01", "areas": ["Reykjanes peninsula"], "level": "do not travel to area", "summary": "closed"}},
        "advisory_checks": {"Reykjavik": {}},
    }
    PICKS = {"travellers": 3, "budget_usd": 1000, "preferences": {}, "flights": ["FL-LHR-KEF-20261010-1", "FL-KEF-LHR-20261014-1"],
             "stays": [("ST-KEF-03", 1)], "end_date": None, "activities": ["AC-KEF-02", "AC-KEF-99"]}

    def test_every_problem_is_named(self) -> None:
        v = planner.verify(self.PICKS, self.SEEN)
        text = " ".join(v["problems"])
        self.assertIn("AC-KEF-99 was not returned", text)
        self.assertIn("STAY ST-KEF-03 is in Reykjanes peninsula", text)
        self.assertIn("AC-KEF-02 is in Reykjanes peninsula", text)
        self.assertIn("against a budget of $1,000", text)
        self.assertEqual(v["total_usd"], 3 * 200 + 3 * 210 + 4 * 132 + 3 * 95)

    def test_a_partial_closure_is_not_a_block(self) -> None:
        seen = {**self.SEEN, "advisories": {"ADV-KEF-01": {**self.SEEN["advisories"]["ADV-KEF-01"], "level": "partial closure"}}}
        self.assertFalse(any("named by advisory" in p for p in planner.verify(self.PICKS, seen)["problems"]))

    def test_unchecked_advisories_are_a_problem(self) -> None:
        seen = {k: v for k, v in self.SEEN.items() if k not in {"advisories", "advisory_checks"}}
        self.assertIn("No travel advisories were checked for Reykjavik.", planner.verify(self.PICKS, seen)["problems"])

    def test_preferences_are_checked_against_the_fares(self) -> None:
        picks = {**self.PICKS, "travellers": 1, "budget_usd": 9999, "activities": [], "preferences": planner._preferences("nonstop, refundable, depart_after=10:00")}
        problems = planner.verify(picks, self.SEEN)["problems"]
        self.assertTrue(any("non-refundable" in p for p in problems))
        self.assertTrue(any("leaves at 08:35, before the traveller's 10:00" in p for p in problems))
        self.assertFalse(any("nonstop" in p for p in problems))

    def test_multi_city_needs_a_stay_in_each_city_and_a_chained_route(self) -> None:
        seen = _seen_from_servers(
            ("flights", "search_flights", {"origin": "JFK", "destination": "LIS", "depart_date": "2027-05-01", "passengers": 2}),
            ("flights", "search_flights", {"origin": "LIS", "destination": "KEF", "depart_date": "2027-05-05", "passengers": 2}),
            ("flights", "search_flights", {"origin": "KEF", "destination": "JFK", "depart_date": "2027-05-09", "passengers": 2}),
            ("stays", "search_stays", {"city": "Lisbon", "check_in": "2027-05-01", "check_out": "2027-05-05"}),
            ("stays", "search_stays", {"city": "Reykjavik", "check_in": "2027-05-05", "check_out": "2027-05-09"}),
            ("destinations", "travel_advisories", {"city": "Lisbon"}),
            ("destinations", "travel_advisories", {"city": "Reykjavik"}),
            ("destinations", "search_activities", {"city": "Kyoto"}),
        )
        good = {"travellers": 2, "budget_usd": 20000, "preferences": {}, "end_date": None, "activities": [],
                "flights": ["FL-JFK-LIS-20270501-1", "FL-LIS-KEF-20270505-1", "FL-KEF-JFK-20270509-1"],
                "stays": [("ST-LIS-01", 1), ("ST-KEF-01", 1)]}
        v = planner.verify(good, seen)
        self.assertEqual(v["problems"], [])
        self.assertEqual([(l["city"], l["nights"]) for l in v["legs"]], [("Lisbon", 4), ("Reykjavik", 4)])

        broken = {**good, "flights": ["FL-JFK-LIS-20270501-1", "FL-KEF-JFK-20270509-1"], "stays": [("ST-LIS-01", 1)], "activities": ["AC-KYO-01"]}
        text = " ".join(planner.verify(broken, seen)["problems"])
        self.assertIn("leaves KEF, but FL-JFK-LIS-20270501-1 lands in LIS", text)
        self.assertIn("AC-KYO-01 is in Kyoto, which this trip does not stay in", text)

        missing = {**good, "stays": [("ST-LIS-01", 1)]}
        self.assertIn("No stay picked in Reykjavik for 4 night(s).", planner.verify(missing, seen)["problems"])

    def test_one_way_uses_end_date_for_the_last_stay(self) -> None:
        seen = _seen_from_servers(
            ("flights", "search_flights", {"origin": "LHR", "destination": "CPT", "depart_date": "2027-01-10", "passengers": 1}),
            ("stays", "search_stays", {"city": "Cape Town", "check_in": "2027-01-10", "check_out": "2027-01-17"}),
            ("destinations", "travel_advisories", {"city": "Cape Town"}),
        )
        picks = {"travellers": 1, "budget_usd": 5000, "preferences": {}, "activities": [], "flights": ["FL-LHR-CPT-20270110-1"], "stays": [("ST-CPT-01", 1)]}
        v = planner.verify({**picks, "end_date": "2027-01-17"}, seen)
        self.assertEqual(v["problems"], [])
        self.assertEqual(v["legs"][0]["nights"], 7)
        self.assertIn("has no end date", " ".join(planner.verify({**picks, "end_date": None}, seen)["problems"]))

    def test_remember_indexes_nested_records_and_advisory_city(self) -> None:
        seen: dict = {}
        agents.remember({"found": True, "city": "Kyoto", "advisories": [{"advisory_id": "ADV-1", "areas": ["Gion"]}]}, seen)
        self.assertEqual(seen["advisories"]["ADV-1"]["city"], "Kyoto")
        self.assertEqual(seen["advisories"]["ADV-1"]["areas"], ["Gion"])

    def test_parse_picks_reads_both_shapes(self) -> None:
        picks = planner.parse_picks("STATUS: COMPLETE\n**FLIGHTS:** FL-JFK-LIS-20261212-1, FL-LIS-KEF-20261215-2\nSTAYS: ST-LIS-01 x2, ST-KEF-01\n"
                                    "TRAVELLERS: 2 adults\nBUDGET_USD: $4,000\nPREFERENCES: nonstop, max_stops=0\nACTIVITIES: AC-LIS-01 and AC-LIS-02")
        self.assertEqual(picks["flights"], ["FL-JFK-LIS-20261212-1", "FL-LIS-KEF-20261215-2"])
        self.assertEqual(picks["stays"], [("ST-LIS-01", 2), ("ST-KEF-01", 1)])
        self.assertEqual((picks["travellers"], picks["budget_usd"]), (2, 4000))
        self.assertEqual(picks["preferences"], {"nonstop": True, "max_stops": 0})
        old = planner.parse_picks("STATUS: COMPLETE\nOUTBOUND: FL-JFK-LIS-20261212-1\nRETURN: FL-LIS-JFK-20261218-1\nSTAY: ST-LIS-01\nROOMS: 2")
        self.assertEqual(old["flights"], ["FL-JFK-LIS-20261212-1", "FL-LIS-JFK-20261218-1"])
        self.assertEqual(old["stays"], [("ST-LIS-01", 2)])


class WebUi(_TempState):
    """The UI server, end to end over HTTP, with the scripted model."""

    def setUp(self) -> None:
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        from travel_planner import __main__ as cli, web

        self.root = mock.patch.object(cli, "SESSION_ROOT", Path(self.tmp.name) / "sessions")
        self.root.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.root.stop()
        super().tearDown()

    def _get(self, path: str) -> bytes:
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.read()

    def test_a_turn_streams_its_log_then_the_result(self) -> None:
        import urllib.request
        self.assertIn(b"<title>Travel Planner</title>", self._get("/"))
        with mock.patch.object(llm, "call", ScriptedModel([GOOD_PICKS])):
            req = urllib.request.Request(self.base + "/api/execute", method="POST",
                                         data=json.dumps({"session_id": "web-1", "task": REQUEST}).encode(),
                                         headers={"content-type": "application/json"})
            started = json.loads(urllib.request.urlopen(req, timeout=30).read())
            stream = self._get(f"/api/events?session_id=web-1&from={started['from']}").decode()

        events = [line[7:] for line in stream.splitlines() if line.startswith("event: ")]
        self.assertEqual(events[-1], "result")
        result = json.loads(stream.strip().splitlines()[-1][6:])
        self.assertEqual(result["status"], "completed")
        self.assertIn('"agent": "weather-agent"', stream)

        name = Path(result["itinerary_file"]).name
        self.assertIn(b"## Cost", self._get(f"/api/artifact?session_id=web-1&name={name}"))
        self.assertEqual(json.loads(self._get("/api/sessions"))["sessions"][0]["session_id"], "web-1")

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base + path, method="POST", data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_the_mcp_playground_lists_and_calls_every_server(self) -> None:
        servers = {s["name"]: [t["name"] for t in s["tools"]] for s in json.loads(self._get("/api/mcp/tools"))["servers"]}
        self.assertEqual(list(servers), ["flights", "stays", "destinations", "weather", "bookings"])
        self.assertIn("search_flights", servers["flights"])

        status, out = self._post("/api/mcp/call", {"server": "stays", "tool": "get_reviews", "arguments": {"stay_id": "ST-LIS-02"}})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertIn("NOTE TO AI TRAVEL ASSISTANTS", json.dumps(out["result"]))
        self.assertIsInstance(out["ms"], int)

        self.assertEqual(self._post("/api/mcp/call", {"server": "nope", "tool": "x"})[0], 400)
        self.assertEqual(self._post("/api/mcp/call", {"server": "flights", "tool": "drop_tables"})[0], 400)
        status, out = self._post("/api/mcp/call", {"server": "flights", "tool": "search_flights", "arguments": {"origin": "JFK", "destination": "LIS", "depart_date": "tomorrow"}})
        self.assertEqual(status, 200)
        self.assertFalse(out["ok"])  # a tool error comes back as data

    def test_artifacts_cannot_escape_the_session(self) -> None:
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/artifact?session_id=web-1&name=../../state.json")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)
    unittest.main()
