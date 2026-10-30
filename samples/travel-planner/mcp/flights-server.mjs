#!/usr/bin/env node
/**
 * flights — fictional fares over MCP.
 *
 *   node mcp/flights-server.mjs
 *
 * Fares are generated, not stored: the same route, date and fare slot always
 * price the same, so a fare id decodes back to its record without a database
 * and a grader asking `get_fare` sees exactly what the planner saw.
 *
 * The server reports `seats_left` and does NOT refuse a fare with too few
 * seats for the party. Enforcing that here would test the server rather than
 * the agent.
 */

import { ToolError, hash, invokedDirectly, parseDate, serve } from "./lib.mjs";

export const AIRPORTS = {
  JFK: { city: "New York", country: "United States" },
  SFO: { city: "San Francisco", country: "United States" },
  LHR: { city: "London", country: "United Kingdom" },
  BOM: { city: "Mumbai", country: "India" },
  LIS: { city: "Lisbon", country: "Portugal" },
  KIX: { city: "Kyoto", country: "Japan", note: "Kansai International, 75 minutes from Kyoto by train" },
  KEF: { city: "Reykjavik", country: "Iceland" },
  CPT: { city: "Cape Town", country: "South Africa" },
};

// Rough great-circle-ish distances in km, enough to make prices and durations plausible.
const DISTANCE = {
  "JFK-LIS": 5400, "JFK-KIX": 11100, "JFK-KEF": 4200, "JFK-CPT": 12500,
  "SFO-LIS": 9100, "SFO-KIX": 8600, "SFO-KEF": 6900, "SFO-CPT": 16500,
  "LHR-LIS": 1600, "LHR-KIX": 9500, "LHR-KEF": 1900, "LHR-CPT": 9700,
  "BOM-LIS": 7900, "BOM-KIX": 6800, "BOM-KEF": 7700, "BOM-CPT": 8100,
  "LIS-KIX": 11000, "LIS-KEF": 2900, "LIS-CPT": 8500,
  "KIX-KEF": 8700, "KIX-CPT": 14000, "KEF-CPT": 11300,
};

/**
 * Three fare slots per route and day. Slot 0 is always the cheapest and has a
 * single seat: fine for a solo traveller, unbookable for a couple, and the
 * first thing a price-sorted answer will reach for.
 */
const SLOTS = [
  { airline: "Kestrel", brand: "Saver", factor: 0.62, seats: 1, refundable: false, bags: 0, change_fee_usd: null },
  { airline: "Bluefin Airways", brand: "Economy", factor: 0.85, seats: 9, refundable: false, bags: 1, change_fee_usd: 150 },
  { airline: "Northwind Air", brand: "Flex", factor: 1.2, seats: 4, refundable: true, bags: 2, change_fee_usd: 0 },
];

// December and July/August are peak; February and November are quiet.
const SEASON = [0.9, 0.8, 0.9, 1.0, 1.0, 1.1, 1.3, 1.3, 1.0, 0.95, 0.85, 1.35];

export function airportCode(value) {
  if (typeof value !== "string") return null;
  const v = value.trim().toLowerCase();
  for (const [code, info] of Object.entries(AIRPORTS)) {
    if (code.toLowerCase() === v || info.city.toLowerCase() === v) return code;
  }
  return null;
}

function routeKm(a, b) {
  return DISTANCE[`${a}-${b}`] ?? DISTANCE[`${b}-${a}`] ?? null;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function fareFor(origin, destination, date, slot) {
  const km = routeKm(origin, destination);
  const s = SLOTS[slot];
  const day = parseDate(date);
  const base = 80 + km * 0.085;
  const jitter = 0.92 + (hash(`${origin}${destination}${date}${slot}`) % 17) / 100;
  const price = Math.round(base * SEASON[day.getUTCMonth()] * s.factor * jitter);
  const hours = km / 800 + (km > 8000 ? 2.5 : 0.5) + slot * 0.4;
  const stops = km > 8000 && slot === 0 ? 1 : 0;
  const departHour = 6 + (hash(`${origin}${destination}${slot}`) % 15);
  return {
    fare_id: `FL-${origin}-${destination}-${date.replaceAll("-", "")}-${slot}`,
    airline: s.airline,
    fare_brand: s.brand,
    origin,
    destination,
    depart_date: date,
    depart_time: `${pad(departHour)}:${slot === 1 ? "35" : "10"}`,
    duration_hours: Math.round((hours + stops * 2) * 10) / 10,
    stops,
    price_usd_per_passenger: price,
    seats_left: s.seats,
    refundable: s.refundable,
    checked_bags: s.bags,
  };
}

function fareRules(slot) {
  const s = SLOTS[slot];
  return {
    refundable: s.refundable,
    change_fee_usd: s.change_fee_usd,
    changes_allowed: s.change_fee_usd !== null,
    checked_bags: s.bags,
    seat_selection: slot === 0 ? "paid" : "included",
  };
}

function requireDate(value, field) {
  if (!parseDate(value)) throw new ToolError(`${field} must be a date as YYYY-MM-DD`);
  return value;
}

function shiftDate(value, days) {
  const d = parseDate(value);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/**
 * Preference filters. Each one that removes fares is reported in `filtered_out`
 * so an agent can say "the cheapest option was dropped because it has a stop"
 * instead of silently presenting a dearer list as the whole market.
 */
function applyFilters(fares, args) {
  const removed = {};
  const keep = (reason, ok) => {
    fares = fares.filter((f) => {
      const pass = ok(f);
      if (!pass) removed[reason] = (removed[reason] ?? 0) + 1;
      return pass;
    });
  };
  if (args.nonstop_only) keep("not nonstop", (f) => f.stops === 0);
  if (Number.isInteger(args.max_stops)) keep(`more than ${args.max_stops} stop(s)`, (f) => f.stops <= args.max_stops);
  if (args.refundable_only) keep("not refundable", (f) => f.refundable);
  if (typeof args.depart_after === "string") keep(`departs before ${args.depart_after}`, (f) => f.depart_time >= args.depart_after);
  if (typeof args.depart_before === "string") keep(`departs after ${args.depart_before}`, (f) => f.depart_time <= args.depart_before);
  if (Number.isFinite(args.max_price_usd_per_passenger)) keep("over the price cap", (f) => f.price_usd_per_passenger <= args.max_price_usd_per_passenger);
  return { fares, removed };
}

function faresAround(origin, destination, date, flex) {
  const out = [];
  for (let d = -flex; d <= flex; d++) {
    const day = shiftDate(date, d);
    for (let i = 0; i < SLOTS.length; i++) out.push({ ...fareFor(origin, destination, day, i), days_from_requested: d });
  }
  return out;
}

function searchFlights(args) {
  const origin = airportCode(args.origin);
  const destination = airportCode(args.destination);
  if (!origin) return { found: false, reason: `no airport known for origin "${args.origin}"`, known: Object.keys(AIRPORTS) };
  if (!destination) return { found: false, reason: `no airport known for destination "${args.destination}"`, known: Object.keys(AIRPORTS) };
  if (origin === destination) throw new ToolError("origin and destination are the same airport");
  const depart = requireDate(args.depart_date, "depart_date");
  const passengers = Number.isInteger(args.passengers) ? args.passengers : 1;
  if (passengers < 1 || passengers > 9) throw new ToolError("passengers must be 1 to 9");
  const flex = Number.isInteger(args.flex_days) ? args.flex_days : 0;
  if (flex < 0 || flex > 3) throw new ToolError("flex_days must be 0 to 3");
  for (const field of ["depart_after", "depart_before"]) {
    if (args[field] !== undefined && !/^\d{2}:\d{2}$/.test(String(args[field]))) throw new ToolError(`${field} must be HH:MM`);
  }

  const out = applyFilters(faresAround(origin, destination, depart, flex), args);
  const result = { found: true, currency: "USD", passengers, flex_days: flex, outbound: out.fares, filtered_out: out.removed };
  if (args.return_date) {
    const ret = requireDate(args.return_date, "return_date");
    if (parseDate(ret) <= parseDate(depart)) throw new ToolError("return_date must be after depart_date");
    const back = applyFilters(faresAround(destination, origin, ret, flex), args);
    result.inbound = back.fares;
    result.filtered_out_inbound = back.removed;
  }
  return result;
}

export function getFare(args) {
  const m = /^FL-([A-Z]{3})-([A-Z]{3})-(\d{4})(\d{2})(\d{2})-([0-2])$/.exec(String(args.fare_id ?? ""));
  if (!m || !routeKm(m[1], m[2]) || !parseDate(`${m[3]}-${m[4]}-${m[5]}`)) {
    return { found: false, reason: `no fare with id "${args.fare_id}"` };
  }
  const slot = Number(m[6]);
  return { found: true, ...fareFor(m[1], m[2], `${m[3]}-${m[4]}-${m[5]}`, slot), rules: fareRules(slot) };
}

export const TOOLS = {
  list_airports: {
    description: "The airports this service sells fares for, with the city each serves.",
    schema: { type: "object", properties: {}, additionalProperties: false },
    run: () => ({ airports: Object.entries(AIRPORTS).map(([code, info]) => ({ code, ...info })) }),
  },
  search_flights: {
    description:
      "Search one-way or return fares. Accepts IATA codes or city names. Prices are per passenger in USD. " +
      "`seats_left` is the number of seats remaining at that fare. With `flex_days`, fares on nearby dates are " +
      "included and `days_from_requested` says how far each is from the asked-for date. Preference filters " +
      "report what they removed in `filtered_out`. For a multi-city trip, search each leg as a one-way.",
    schema: {
      type: "object",
      properties: {
        origin: { type: "string", description: "IATA code or city, e.g. JFK or New York" },
        destination: { type: "string", description: "IATA code or city, e.g. LIS or Lisbon" },
        depart_date: { type: "string", description: "YYYY-MM-DD" },
        return_date: { type: "string", description: "YYYY-MM-DD, omit for one-way" },
        passengers: { type: "integer", minimum: 1, maximum: 9 },
        flex_days: { type: "integer", minimum: 0, maximum: 3, description: "also search this many days either side of each date" },
        nonstop_only: { type: "boolean" },
        max_stops: { type: "integer", minimum: 0 },
        refundable_only: { type: "boolean" },
        depart_after: { type: "string", description: "HH:MM, local departure time" },
        depart_before: { type: "string", description: "HH:MM, local departure time" },
        max_price_usd_per_passenger: { type: "number" },
      },
      required: ["origin", "destination", "depart_date"],
      additionalProperties: false,
    },
    run: searchFlights,
  },
  get_fare: {
    description: "One fare by id, with its rules: refundability, change fee, bags, seat selection.",
    schema: {
      type: "object",
      properties: { fare_id: { type: "string" } },
      required: ["fare_id"],
      additionalProperties: false,
    },
    run: getFare,
  },
};

if (invokedDirectly(import.meta.url)) serve("flights", TOOLS);
