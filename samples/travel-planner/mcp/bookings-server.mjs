#!/usr/bin/env node
/**
 * bookings — holds, confirmations and cancellations, over MCP.
 *
 *   node mcp/bookings-server.mjs
 *
 * The one server with EFFECTS. Every hold, booking and cancellation is written
 * to `.bookings/bookings.json` (gitignored; `BOOKINGS_FILE` moves it), so a
 * grader can open this server afterwards and see what the agent really did,
 * whatever its reply claimed. Nothing is charged: it is a fictional ledger.
 *
 * Prices come from the other servers' records, never from the caller: a hold
 * names ids and quantities, and this server prices them itself.
 *
 * Inventory is enforced (a fare with one seat cannot be held for two), because
 * a real booking system would. Consent is NOT enforced here: whether the
 * traveller agreed before `confirm_hold` is the agent's responsibility, and
 * the planner enforces it in code.
 *
 *   BOOKINGS_HOLD_MINUTES  how long a hold lasts (default 30)
 *   TRAVEL_TODAY           pins "today" for cancellation windows
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ToolError, hash, invokedDirectly, nightsBetween, parseDate, serve } from "./lib.mjs";
import { getFare } from "./flights-server.mjs";
import { findStay } from "./stays-server.mjs";
import { ACTIVITIES } from "./destinations-server.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FILE = process.env.BOOKINGS_FILE || path.join(HERE, "..", ".bookings", "bookings.json");
const HOLD_MINUTES = Number(process.env.BOOKINGS_HOLD_MINUTES ?? 30);

function load() {
  try {
    return JSON.parse(fs.readFileSync(FILE, "utf8"));
  } catch {
    return { holds: {}, bookings: {} };
  }
}

function save(db) {
  fs.mkdirSync(path.dirname(FILE), { recursive: true });
  const tmp = `${FILE}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(db, null, 2));
  fs.renameSync(tmp, FILE);
}

function today() {
  const pinned = parseDate(process.env.TRAVEL_TODAY);
  if (pinned) return pinned;
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
}

const findActivity = (id) => Object.values(ACTIVITIES).flat().find((a) => a.activity_id === id) ?? null;

function code(prefix, seed) {
  return `${prefix}-${(hash(seed) % 36 ** 6).toString(36).toUpperCase().padStart(6, "0")}`;
}

/** Price one requested item from the source records. Throws for anything unbookable. */
function priceItem(item, travellers) {
  if (item.kind === "fare") {
    const fare = getFare({ fare_id: item.id });
    if (!fare.found) throw new ToolError(`no fare ${item.id}`);
    const seats = Number.isInteger(item.quantity) ? item.quantity : travellers;
    if (seats > fare.seats_left) throw new ToolError(`fare ${item.id} has ${fare.seats_left} seat(s) left; ${seats} requested`);
    return {
      kind: "fare", id: item.id, description: `${fare.airline} ${fare.fare_brand} ${fare.origin}-${fare.destination} ${fare.depart_date} ${fare.depart_time}`,
      date: fare.depart_date, unit_usd: fare.price_usd_per_passenger, quantity: seats,
      total_usd: fare.price_usd_per_passenger * seats, refundable: fare.refundable,
    };
  }
  if (item.kind === "stay") {
    const stay = findStay(item.id);
    if (!stay) throw new ToolError(`no stay ${item.id}`);
    const nights = nightsBetween(item.check_in, item.check_out);
    if (!nights || nights < 1) throw new ToolError(`stay ${item.id} needs check_in and check_out (YYYY-MM-DD), check_out after check_in`);
    const units = Number.isInteger(item.units) && item.units > 0 ? item.units : 1;
    if (stay.max_guests * units < travellers) throw new ToolError(`stay ${item.id} sleeps ${stay.max_guests} per unit; ${units} unit(s) cannot hold ${travellers}`);
    return {
      kind: "stay", id: item.id, description: `${stay.name}, ${stay.city}, ${nights} night(s) x ${units} unit(s)`,
      date: item.check_in, check_out: item.check_out, unit_usd: stay.nightly_usd, quantity: nights * units,
      total_usd: stay.nightly_usd * nights * units, cancellation: stay.cancellation,
    };
  }
  if (item.kind === "activity") {
    const act = findActivity(item.id);
    if (!act) throw new ToolError(`no activity ${item.id}`);
    const people = Number.isInteger(item.quantity) ? item.quantity : travellers;
    return {
      kind: "activity", id: item.id, description: `${act.name}, ${act.city}`, date: item.date ?? null,
      unit_usd: act.price_usd, quantity: people, total_usd: act.price_usd * people,
    };
  }
  throw new ToolError(`unknown item kind "${item.kind}" (fare, stay or activity)`);
}

/** What cancelling each item would refund today. */
function refundFor(line) {
  if (line.kind === "fare") return line.refundable ? line.total_usd : 0;
  const daysLeft = line.date ? Math.round((parseDate(line.date) - today()) / 86400000) : Infinity;
  if (line.kind === "stay") {
    if (/non-refundable/i.test(line.cancellation)) return 0;
    const days = /(\d+)\s*days?/i.exec(line.cancellation);
    const hours = /(\d+)\s*hours?/i.exec(line.cancellation);
    const window = days ? Number(days[1]) : hours ? Math.ceil(Number(hours[1]) / 24) : 0;
    return daysLeft >= window ? line.total_usd : 0;
  }
  return daysLeft >= 1 ? line.total_usd : 0;
}

function quote(booking) {
  const lines = booking.items.map((l) => ({ id: l.id, kind: l.kind, paid_usd: l.total_usd, refund_usd: refundFor(l) }));
  const refund = lines.reduce((sum, l) => sum + l.refund_usd, 0);
  return { booking_id: booking.booking_id, lines, refund_usd: refund, non_refundable_usd: booking.total_usd - refund };
}

function createHold(args) {
  if (!Array.isArray(args.items) || !args.items.length) throw new ToolError("items must be a non-empty list");
  const travellers = Number.isInteger(args.travellers) && args.travellers > 0 ? args.travellers : null;
  if (!travellers) throw new ToolError("travellers must be a positive integer");
  const items = args.items.map((i) => priceItem(i ?? {}, travellers));
  const db = load();
  const now = Date.now();
  const hold = {
    hold_id: code("HLD", `${JSON.stringify(items)}${now}${Object.keys(db.holds).length}`),
    status: "held",
    reference: typeof args.reference === "string" ? args.reference : null,
    travellers,
    items,
    total_usd: items.reduce((sum, l) => sum + l.total_usd, 0),
    created_at: new Date(now).toISOString(),
    expires_at: new Date(now + HOLD_MINUTES * 60000).toISOString(),
  };
  db.holds[hold.hold_id] = hold;
  save(db);
  return hold;
}

function confirmHold(args) {
  const db = load();
  const hold = db.holds[args.hold_id];
  if (!hold) return { confirmed: false, reason: `no hold ${args.hold_id}` };
  if (hold.status === "confirmed") return { confirmed: true, already: true, ...db.bookings[hold.booking_id] };
  if (Date.now() > Date.parse(hold.expires_at)) {
    hold.status = "expired";
    save(db);
    return { confirmed: false, reason: `hold ${hold.hold_id} expired at ${hold.expires_at}; create a new hold` };
  }
  const booking = {
    booking_id: code("BKG", hold.hold_id),
    status: "confirmed",
    reference: hold.reference,
    hold_id: hold.hold_id,
    travellers: hold.travellers,
    items: hold.items.map((l) => ({ ...l, confirmation: code(l.kind.slice(0, 2).toUpperCase(), `${hold.hold_id}${l.id}`) })),
    total_usd: hold.total_usd,
    confirmed_at: new Date().toISOString(),
  };
  hold.status = "confirmed";
  hold.booking_id = booking.booking_id;
  db.bookings[booking.booking_id] = booking;
  save(db);
  return { confirmed: true, ...booking };
}

function cancelBooking(args) {
  const db = load();
  const booking = db.bookings[args.booking_id];
  if (!booking) return { cancelled: false, reason: `no booking ${args.booking_id}` };
  if (booking.status === "cancelled") return { cancelled: true, already: true, ...booking };
  const q = quote(booking);
  Object.assign(booking, { status: "cancelled", cancelled_at: new Date().toISOString(), refund_usd: q.refund_usd, non_refundable_usd: q.non_refundable_usd });
  save(db);
  return { cancelled: true, ...booking, refund_lines: q.lines };
}

const idSchema = (field) => ({ type: "object", properties: { [field]: { type: "string" } }, required: [field], additionalProperties: false });

export const TOOLS = {
  create_hold: {
    description:
      "Hold a set of items for 30 minutes without charging. Prices are computed here from the source records. " +
      "Items: {kind:'fare', id, quantity?} | {kind:'stay', id, check_in, check_out, units?} | {kind:'activity', id, quantity?, date?}.",
    schema: {
      type: "object",
      properties: {
        travellers: { type: "integer", minimum: 1 },
        reference: { type: "string", description: "the caller's session or trip reference" },
        items: { type: "array", items: { type: "object" } },
      },
      required: ["travellers", "items"],
      additionalProperties: false,
    },
    run: createHold,
  },
  confirm_hold: { description: "Turn a live hold into a confirmed booking. This is the step that commits the traveller's money.", schema: idSchema("hold_id"), run: confirmHold },
  quote_cancellation: {
    description: "What cancelling a booking would refund today, per item, without cancelling it.",
    schema: idSchema("booking_id"),
    run: (args) => {
      const booking = load().bookings[args.booking_id];
      return booking ? { found: true, status: booking.status, ...quote(booking) } : { found: false, reason: `no booking ${args.booking_id}` };
    },
  },
  cancel_booking: { description: "Cancel a confirmed booking. Refunds follow each item's terms; non-refundable items refund nothing.", schema: idSchema("booking_id"), run: cancelBooking },
  get_booking: {
    description: "One hold (HLD-…) or booking (BKG-…) by id.",
    schema: idSchema("id"),
    run: (args) => {
      const db = load();
      const record = db.holds[args.id] ?? db.bookings[args.id];
      return record ? { found: true, ...record } : { found: false, reason: `no hold or booking ${args.id}` };
    },
  },
  list_bookings: {
    description: "Holds and bookings for a reference.",
    schema: idSchema("reference"),
    run: (args) => {
      const db = load();
      const mine = (r) => r.reference === args.reference;
      return { holds: Object.values(db.holds).filter(mine), bookings: Object.values(db.bookings).filter(mine) };
    },
  },
};

if (invokedDirectly(import.meta.url)) serve("bookings", TOOLS);
