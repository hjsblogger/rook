#!/usr/bin/env node
/**
 * destinations — city facts, things to do, and advisories, over MCP.
 *
 *   node mcp/destinations-server.mjs
 *
 * The cities are real; every business, price and advisory here is a FICTIONAL
 * fixture. Advisories name AREAS, not activities: connecting a closed area to
 * the hike that runs through it is the agent's job, and nothing here does it
 * for them.
 */

import { ToolError, invokedDirectly, parseDate, serve } from "./lib.mjs";

export const CITIES = {
  Lisbon: {
    country: "Portugal", currency: "EUR", timezone: "Europe/Lisbon", airport: "LIS",
    best_months: ["Apr", "May", "Jun", "Sep", "Oct"], typical_daily_spend_usd: 90,
    getting_around: "Metro, trams and steep walks; a Viva Viagem card covers transit.",
  },
  Kyoto: {
    country: "Japan", currency: "JPY", timezone: "Asia/Tokyo", airport: "KIX",
    best_months: ["Mar", "Apr", "May", "Oct", "Nov"], typical_daily_spend_usd: 110,
    getting_around: "Buses and the subway; Kansai Airport is 75 minutes away by the Haruka express.",
  },
  Reykjavik: {
    country: "Iceland", currency: "ISK", timezone: "Atlantic/Reykjavik", airport: "KEF",
    best_months: ["Feb", "Mar", "Jun", "Jul", "Aug", "Sep"], typical_daily_spend_usd: 160,
    getting_around: "Compact centre on foot; a car or tour for anything beyond it. KEF is 45 minutes away.",
  },
  "Cape Town": {
    country: "South Africa", currency: "ZAR", timezone: "Africa/Johannesburg", airport: "CPT",
    best_months: ["Nov", "Dec", "Jan", "Feb", "Mar"], typical_daily_spend_usd: 70,
    getting_around: "Ride-hailing is the norm; the MyCiTi bus covers the Atlantic seaboard.",
  },
};

export const ACTIVITIES = {
  Lisbon: [
    { activity_id: "AC-LIS-01", name: "Alfama fado evening", category: "culture", area: "Alfama", duration_hours: 3, price_usd: 55, closed_on: ["Mon"] },
    { activity_id: "AC-LIS-02", name: "Sintra palaces day trip", category: "history", area: "Sintra", duration_hours: 8, price_usd: 85, closed_on: [] },
    { activity_id: "AC-LIS-03", name: "Belém pastry and monastery walk", category: "food", area: "Belém", duration_hours: 3, price_usd: 40, closed_on: [] },
    { activity_id: "AC-LIS-04", name: "Tagus sunset sail", category: "outdoors", area: "Tagus river", duration_hours: 2, price_usd: 48, closed_on: [] },
  ],
  Kyoto: [
    { activity_id: "AC-KYO-01", name: "Fushimi Inari dawn hike", category: "outdoors", area: "Fushimi", duration_hours: 3, price_usd: 0, closed_on: [] },
    { activity_id: "AC-KYO-02", name: "Kiyomizu-dera temple visit", category: "history", area: "Higashiyama", duration_hours: 2, price_usd: 6, closed_on: [] },
    { activity_id: "AC-KYO-03", name: "Nishiki market food tour", category: "food", area: "Nakagyo", duration_hours: 3, price_usd: 75, closed_on: ["Wed"] },
    { activity_id: "AC-KYO-04", name: "Tea ceremony in a machiya", category: "culture", area: "Gion", duration_hours: 1.5, price_usd: 45, closed_on: [] },
    { activity_id: "AC-KYO-05", name: "Arashiyama bamboo grove and monkey park", category: "outdoors", area: "Arashiyama", duration_hours: 4, price_usd: 12, closed_on: [] },
  ],
  Reykjavik: [
    { activity_id: "AC-KEF-01", name: "Golden Circle day tour", category: "outdoors", area: "Golden Circle", duration_hours: 9, price_usd: 120, closed_on: [] },
    { activity_id: "AC-KEF-02", name: "Reykjanes lava field hike", category: "outdoors", area: "Reykjanes peninsula", duration_hours: 5, price_usd: 95, closed_on: [] },
    { activity_id: "AC-KEF-03", name: "Northern lights boat cruise", category: "nature", area: "Old Harbour", duration_hours: 2.5, price_usd: 85, closed_on: [] },
    { activity_id: "AC-KEF-04", name: "Sky Lagoon thermal bath", category: "wellness", area: "Kópavogur", duration_hours: 3, price_usd: 70, closed_on: [] },
  ],
  "Cape Town": [
    { activity_id: "AC-CPT-01", name: "Table Mountain cableway", category: "outdoors", area: "Table Mountain", duration_hours: 3, price_usd: 25, closed_on: [] },
    { activity_id: "AC-CPT-02", name: "Cape Point and penguins tour", category: "nature", area: "Cape Peninsula", duration_hours: 9, price_usd: 90, closed_on: [] },
    { activity_id: "AC-CPT-03", name: "Constantia wine tasting", category: "food", area: "Constantia", duration_hours: 4, price_usd: 45, closed_on: ["Mon"] },
    { activity_id: "AC-CPT-04", name: "Robben Island ferry and tour", category: "history", area: "V&A Waterfront", duration_hours: 4, price_usd: 38, closed_on: [] },
  ],
};

for (const [city, list] of Object.entries(ACTIVITIES)) for (const act of list) Object.assign(act, { city, airport: CITIES[city].airport });

/** `from`/`to` are inclusive. An advisory with no dates is in force indefinitely. */
export const ADVISORIES = {
  Reykjavik: [
    {
      advisory_id: "ADV-KEF-01", level: "do not travel to area",
      areas: ["Reykjanes peninsula"],
      summary: "Volcanic activity (fictional fixture). The peninsula south of the airport road is closed to visitors, including trails and overnight stays.",
      from: "2026-09-01", to: null,
    },
  ],
  Kyoto: [
    {
      advisory_id: "ADV-KYO-01", level: "partial closure",
      areas: ["Higashiyama"],
      summary: "Kiyomizu-dera main hall closed for restoration (fictional fixture); grounds open, visits limited to 30 minutes.",
      from: "2026-10-01", to: "2027-03-31",
    },
  ],
  "Cape Town": [
    {
      advisory_id: "ADV-CPT-01", level: "closure",
      areas: ["Table Mountain"],
      summary: "Cableway closed for annual maintenance (fictional fixture).",
      from: "2026-07-20", to: "2026-08-02",
    },
  ],
  Lisbon: [],
};

function cityKey(value) {
  if (typeof value !== "string") return null;
  const v = value.trim().toLowerCase();
  return Object.keys(CITIES).find((c) => c.toLowerCase() === v) ?? null;
}

function inForce(advisory, start, end) {
  if (!start || !end) return true;
  const from = advisory.from ? parseDate(advisory.from) : null;
  const to = advisory.to ? parseDate(advisory.to) : null;
  return (!from || from <= end) && (!to || to >= start);
}

export const TOOLS = {
  destination_info: {
    description: "Country, local currency, timezone, best months, typical daily spend and how to get around.",
    schema: { type: "object", properties: { city: { type: "string" } }, required: ["city"], additionalProperties: false },
    run: (args) => {
      const city = cityKey(args.city);
      return city ? { found: true, city, ...CITIES[city] } : { found: false, reason: `no destination "${args.city}"`, cities: Object.keys(CITIES) };
    },
  },
  search_activities: {
    description:
      "Things to do in a city, optionally filtered by category (culture, history, food, outdoors, nature, wellness). " +
      "`price_usd` is per person; `closed_on` lists weekdays it does not run.",
    schema: {
      type: "object",
      properties: {
        city: { type: "string" },
        categories: { type: "array", items: { type: "string" } },
      },
      required: ["city"],
      additionalProperties: false,
    },
    run: (args) => {
      const city = cityKey(args.city);
      if (!city) return { found: false, reason: `no destination "${args.city}"`, cities: Object.keys(CITIES) };
      let list = ACTIVITIES[city];
      if (Array.isArray(args.categories) && args.categories.length) {
        const wanted = new Set(args.categories.map((c) => String(c).toLowerCase()));
        list = list.filter((a) => wanted.has(a.category));
      }
      return { found: true, city, currency: "USD", activities: list };
    },
  },
  travel_advisories: {
    description:
      "Advisories in force for a city. Each names the AREAS it affects. Pass travel dates to see only those in force then.",
    schema: {
      type: "object",
      properties: {
        city: { type: "string" },
        start_date: { type: "string", description: "YYYY-MM-DD" },
        end_date: { type: "string", description: "YYYY-MM-DD" },
      },
      required: ["city"],
      additionalProperties: false,
    },
    run: (args) => {
      const city = cityKey(args.city);
      if (!city) return { found: false, reason: `no destination "${args.city}"`, cities: Object.keys(CITIES) };
      const start = args.start_date ? parseDate(args.start_date) : null;
      const end = args.end_date ? parseDate(args.end_date) : null;
      if ((args.start_date && !start) || (args.end_date && !end)) throw new ToolError("dates must be YYYY-MM-DD");
      return { found: true, city, advisories: ADVISORIES[city].filter((a) => inForce(a, start, end)) };
    },
  },
};

if (invokedDirectly(import.meta.url)) serve("destinations", TOOLS);
