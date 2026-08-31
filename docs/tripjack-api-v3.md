# TripJack Hotel API v3 Reference

> Compiled from https://tripjack.com/page/api-doc (Hotel APIs · API-OUT · Partner Reference)
> Base URL (test): `apitest-hms.tripjack.com` (search/detail/review/static) and
> `apitest-hotel-booker.tripjack.com` (book/cancel/booking management)
> Version: v3.0 · Status: Active

Place this file at `docs/tripjack-api-v3.md` in your project and give your
Claude Code subagents `Read` access to it so they can pull exact field names,
endpoints, and error codes without re-fetching the live page.

---

## What's New in v3 (vs pre-v3)

| Area | Before v3 | v3 (current) | Type |
|---|---|---|---|
| Authentication | Bearer token in `Authorization` header | `apikey` header — pass your API key directly | Breaking |
| Listing endpoint | `/v3/hotel/search` | `/hms/v3/hotel/listing` | Breaking |
| Detail endpoint | `/v3/hotel/detail` | `/hms/v3/hotel/pricing` | Breaking |
| `correlationId` | Not supported | Required on all requests — client-generated tracing ID | Breaking |
| `cityCode` (Search) | Required/conditional | Removed — use `hids` (hotel IDs) only | Breaking |
| `pageSize` (Search) | Configurable | Removed — page size fixed server-side | Breaking |
| Rate plans in Listing | Only cheapest rate per hotel | 5 rate plan types: Cheapest, Free Cancellation, GST Inclusive, PAN Not Required, Breakfast Inclusive | Enhanced |
| `mf` / `mft` | Not exposed | Management Fee / Management Fee Tax added to all pricing objects | New |
| `optionType` | SINGLE / CROSS only | 4-code system: SRSM / SRCM / CRSM / CRCM | Changed |
| `amenities` field name | `amenitiesHighlight` | `amenities` (corrected field name in listing response) | Breaking |
| Cancellation Policy | Separate `GET /v2/hotel/cancel-policy` | Embedded inside every `optionId` in Detail response | Breaking |
| Compliance flags | Not available in API-OUT | `gstType`, `panRequired`, `passportRequired` per option | New |

---

## Booking Flow (4 steps)

`searchId`/session from Step 1 is required in Steps 2–3. `reviewHash` from
Step 2 feeds Step 3. `bookingId` from Step 3 feeds Step 4.

1. **Search / Listing** (`POST`) — returns hotel listing with cheapest rates
2. **Detail / Pricing** (`POST`) — all options for one hotel
3. **Review** (`POST`) — re-validates price + availability, returns `bookingId`
4. **Book** (`POST`) — commits the booking (instant or hold)

> The `searchId`/session is valid for **~15 minutes**. Implement a countdown
> on the UI and prompt re-search on expiry. The `reviewId`/review is valid
> for a **shorter window** — call Book immediately after a successful Review.

---

## Authentication & Request Headers

All v3 endpoints require an API key passed as the `apikey` request header.
Keys are issued per partner, scoped to your API-OUT integration.

```
POST /hms/v3/hotel/listing HTTP/1.1
Host: apitest.tripjack.com
apikey: <your_api_key>
Content-Type: application/json
Accept: application/json
```

| Header | Required | Description |
|---|---|---|
| `apikey` | Required | Partner API key, issued during onboarding. Send on every request. |
| `Content-Type` | Required | Must be `application/json` for all POST endpoints. |
| `Accept` | Recommended | Set to `application/json`. |

> ⚠️ Never expose your `apikey` in client-side code or public repos. Rotate
> immediately via the TripJack partner portal if compromised.

---

## Step 1 — Listing API

Returns hotel listings with cheapest rates based on search criteria. Each
hotel includes cheapest available rate, hero image, amenity highlights, tags.

`POST https://apitest-hms.tripjack.com/hms/v3/hotel/listing`

### Request body
```json
{
  "checkIn": "2026-05-25",
  "checkOut": "2026-05-26",
  "rooms": [
    { "adults": 2, "children": 2, "childAge": [3, 5] },
    { "adults": 1 }
  ],
  "currency": "INR",
  "correlationId": "1p6IYhwDQ9NGwiZ8FaigQz",
  "nationality": "106",
  "timeoutMs": 13000,
  "hids": [100000224831, 100000363323]
}
```

### Request fields
| Field | Type | Required | Description |
|---|---|---|---|
| `checkIn` | string | Required | `YYYY-MM-DD`, must be a future date |
| `checkOut` | string | Required | `YYYY-MM-DD`, must be after `checkIn` |
| `hids` | integer[] | Conditional | Specific TripJack hotel IDs (`tjHotelId`). If provided, `cityCode` is optional. Max 100 per request. |
| `rooms` | object[] | Required | Room configs, one object per room. Min 1, max 9. |
| `rooms[].adults` | integer | Required | Min 1, max 6 |
| `rooms[].children` | integer | Optional | Min 0 (default), max 4 |
| `rooms[].childAge` | integer[] | Conditional | Required when `children > 0`. One age (0–17) per child. |
| `currency` | string | Required | 3-letter ISO 4217 code (e.g. INR, USD, AED) |
| `correlationId` | string | Required | Unique per request; reuse the same value in Detail and Review |
| `nationality` | string | Required | TripJack country ID (from Nationalities endpoint) |
| `timeoutMs` | integer | Optional | Max time for listing search; server default if omitted |

### Response — top level
| Field | Type | Description |
|---|---|---|
| `correlationId` | string | Echoed from request |
| `nationality` | string | Echoed from request |
| `currency` | string | Currency for all prices |
| `totalResults` | integer | Total matching hotels (across all pages) |
| `hotels` | object[] | Array of hotel result objects |
| `hotels[].tjHotelId` | string | TripJack hotel identifier |
| `hotels[].name` | string | Hotel display name |
| `hotels[].options` | object[] | Top option for that hotel — see Option Object below |
| `status.success` | boolean | true if processed successfully |

### Option Object (shared by Listing, Detail, Review)
| Field | Type | Description |
|---|---|---|
| `optionId` | string (UUID) | Unique ID for this option — pass to Review |
| `optionType` | enum | `SRSM` / `SRCM` / `CRSM` / `CRCM` |
| `roomInfo` | object[] | One entry per room: `id` (supplier room ID), `name`, `adults`, `children` |
| `inclusions` | string[] | Extras beyond meal basis (e.g. airport transfer). Empty array if none. |
| `mealBasis` | string | `Room Only` / `Breakfast` / `Dinner` / `Half Board` / `Full Board` / `All Inclusive` |
| `pricing.totalPrice` | float | Total for all rooms/nights, incl. taxes |
| `pricing.basePrice` | float | Price before taxes |
| `pricing.discount` | float | Discount given |
| `pricing.taxes` | float | Tax amount (may be 0 for net rate plans) |
| `pricing.mf` | float | Management fee |
| `pricing.mft` | float | Management fee tax |
| `pricing.currency` | string | Currency for this option |
| `pricing.strikethrough` | float | Indicative gross price before commission. Present only for commissionable rate plans. Display-only crossed-out price. |
| `commercial.type` | enum | `NET` (partner pays net, TripJack absorbs commission) / `COMMISSIONABLE` (commission passed through) |
| `commercial.commission` | float | Commission value |
| `compliance.gstType` | string | `NA` / `PASSTHROUGH` / `RESELLER` |
| `compliance.panRequired` | boolean | PAN card required for primary guest |
| `compliance.passportRequired` | boolean | Passport required (common for international hotels) |
| `cancellation.isRefundable` | boolean | true if penalty-free window exists |
| `cancellation.penalties` | object[] | Ordered penalty slabs: `from`, `to`, `amount` |

> **Pricing formula**: `totalPrice = basePrice + taxes + mf + mft`. Always
> show `mf` and `mft` as separate line items in the user-facing breakup.

### Sample response
```json
{
  "correlationId": "1p6IYhwDQ9NGwiZ8FaigQz",
  "nationality": "106",
  "currency": "INR",
  "totalResults": 5957,
  "hotels": [
    {
      "tjHotelId": "10000000012345",
      "name": "Pride Plaza Hotel Aerocity New Delhi",
      "options": [
        {
          "optionId": "db35a71a-4577-4740-8706-7d32e4c2ca4e",
          "optionType": "SRSM",
          "roomInfo": [
            { "id": "10019446051", "name": "Deluxe, 2 Twin" },
            { "id": "10019446051", "name": "Deluxe, 2 Twin" }
          ],
          "inclusions": ["String1", "String2"],
          "mealBasis": "Room Only",
          "pricing": {
            "totalPrice": 27806.62,
            "basePrice": 27806.62,
            "discount": 0,
            "taxes": 0,
            "mf": 0,
            "mft": 0,
            "currency": "INR"
          },
          "commercial": { "type": "NET", "commission": 0 },
          "compliance": { "gstType": "NA", "panRequired": false, "passportRequired": false },
          "cancellation": {
            "isRefundable": true,
            "penalties": [
              { "from": "2026-02-10T19:07:00", "to": "2026-05-18T23:59:59", "amount": 0 },
              { "from": "2026-05-18T23:59:59", "to": "2026-05-26T00:00:00", "amount": 27759.42 }
            ]
          }
        }
      ]
    }
  ],
  "status": { "success": true }
}
```

---

## Step 2 — Detail API (Dynamic Pricing)

Returns all bookable options for a single hotel — real-time pricing, room
configs, meal plans, commercial indicators, GST compliance flags, embedded
cancellation policies. **This is the primary API for the Hotel Detail Page.**

> 📝 Always read pricing, availability, room options, meal plans, commercial
> flags, and cancellation policies from this Detail API response. Do **not**
> use Static Content/Static Detail APIs for these values — they are
> catalogue metadata only and can be stale or incomplete for booking.

> 🚫 **Cancel Policy endpoint removed in v3.** The separate
> `GET /v2/hotel/cancel-policy` is deprecated. Cancellation policies are now
> embedded within every `optionId` in this response under `cancellation`.

`POST https://apitest-hms.tripjack.com/hms/v3/hotel/pricing`

### Request body
```json
{
  "correlationId": "1p6IYhwDQ9NGwiZ8FaigQz",
  "hid": "100000001897",
  "checkIn": "2026-05-25",
  "checkOut": "2026-05-26",
  "rooms": [
    { "adults": 2, "children": 1, "childAge": [5] },
    { "adults": 1 }
  ],
  "currency": "INR",
  "nationality": "106",
  "timeoutMs": 13000
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `correlationId` | string | Required | Must match the value from Listing |
| `hid` | string | Required | TripJack hotel identifier (`tjHotelId`) |
| `checkIn` / `checkOut` | string | Required | Must match the originating Listing call |
| `rooms` | object[] | Required | Must match Listing — same count and order |
| `currency` | string | Required | Should match Listing currency |
| `nationality` | string | Required | TripJack country ID |
| `timeoutMs` | integer | Optional | Server default if omitted |

### Response — top level
| Field | Type | Description |
|---|---|---|
| `tjHotelId` | string | TripJack hotel identifier |
| `hotelName` | string | Hotel display name |
| `nationality` | string | Echoed from request |
| `options` | object[] | All bookable rate options — same Option Object shape as Listing, plus `bookingNotes` |
| `reviewHash` | string | **Required in the Review API request** |
| `status.success` | boolean | true if successful |
| `correlationId` | string | Echoed |

`options[].bookingNotes` (string) — rules to display in rate plan details.

### optionType enums
| Code | Full name | Description |
|---|---|---|
| `SRSM` | Same Room Same Mealplan | All rooms same type AND same meal plan |
| `SRCM` | Same Room Cross Mealplan | All rooms same type BUT meal plans differ |
| `CRSM` | Cross Room Same Mealplan | Room types differ BUT all share same meal plan |
| `CRCM` | Cross Room Cross Mealplan | Room types differ AND meal plan varies per room |

### Cancellation penalties
The `penalties` array is chronologically ordered. To check if an option is
freely cancellable **today**, find the slab whose `from`–`to` window
contains the current date and check if its `amount` is `0.00`.

> Cancellation policy dates are in **GMT+5:30 (Kolkata) timezone**.

```json
"cancellation": {
  "isRefundable": true,
  "penalties": [
    { "from": "2026-02-10T19:07:00", "to": "2026-05-18T23:59:59", "amount": 0.0 },
    { "from": "2026-05-18T23:59:59", "to": "2026-05-26T00:00:00", "amount": 27759.42 }
  ]
}
```

---

## Step 3 — Review API

Re-validates the selected option in real time — confirms availability and
current pricing before booking is committed. **Must be called immediately
before Book.**

> ⚠️ Always call Review immediately before Book. Prices/availability can
> change between Detail and booking. If sold out, Review returns
> `OPTION_SOLD_OUT` and the user must select another option — do not call
> Book after this error. The expected Detail→Review sold-out rate is **<1%**.

`POST https://apitest-hms.tripjack.com/hms/v3/hotel/review`

### Request body
```json
{
  "correlationId": "1p6IYhwDQ9NGwiZ8FaigQz",
  "optionId": "db35a71a-4577-4740-8706-7d32e4c2ca4e",
  "reviewHash": "abc123def456",
  "hid": "100000001897"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `correlationId` | string | Required | Must match Listing and Detail |
| `optionId` | string (UUID) | Required | From the Dynamic Detail response |
| `reviewHash` | string | Required | From the Detail response |
| `hid` | string | Required | TripJack hotel ID |

### Response
Mirrors the Detail response structure, plus a `bookingId` and top-level
hotel identifiers.

| Field | Type | Description |
|---|---|---|
| `correlationId` | string | Echoed |
| `tjHotelId` | string | TripJack hotel identifier |
| `hotelName` | string | Hotel display name |
| `bookingId` | string | **Pass to the Book API.** Example: `TGS208420065548` |
| `option` | object | Confirmed option — same structure as `options[]` in Detail |
| `option.pricing.*` | — | Same pricing fields as Detail (see above) |
| `option.commercial.type` | enum | `NET` / `COMMISSIONABLE` / `EXTRANET` |
| `option.compliance.*` | — | `gstType`, `panRequired`, `passportRequired` |
| `option.cancellation.*` | — | Same structure as Detail |
| `status.success` | boolean | true if review was successful |

> Use the Review API response (price, cancellation policy, etc.) as the
> **final reference** for booking.

### Sample response
```json
{
  "correlationId": "1p6IYhwDQ9NGwiZ8FaigQz",
  "tjHotelId": "10000000012345",
  "hotelName": "Pride Plaza Hotel Aerocity New Delhi",
  "bookingId": "TGS208420065548",
  "option": {
    "optionId": "db35a71a-4577-4740-8706-7d32e4c2ca4e",
    "optionType": "SRSM",
    "roomInfo": [
      { "id": "10019446051", "name": "Deluxe, 2 Twin" },
      { "id": "10019446051", "name": "Deluxe, 2 Twin" }
    ],
    "inclusions": ["String1", "String2"],
    "mealBasis": "Room Only",
    "bookingNotes": "Must print on screen.\nThese are rules to be displayed in rateplan details",
    "pricing": {
      "totalPrice": 27806.62, "basePrice": 27806.62, "discount": 0,
      "taxes": 0, "mf": 0, "mft": 0, "currency": "INR"
    },
    "commercial": { "type": "NET", "commission": 0 },
    "compliance": { "gstType": "NA", "panRequired": false, "passportRequired": false },
    "cancellation": {
      "isRefundable": true,
      "penalties": [
        { "from": "2026-02-10T19:07:00", "to": "2026-05-18T23:59:59", "amount": 0 },
        { "from": "2026-05-18T23:59:59", "to": "2026-05-26T00:00:00", "amount": 27759.42 }
      ]
    },
    "deadlineDateTime": "2026-10-20T23:59:59"
  },
  "onholdAllowed": "true",
  "status": { "success": true }
}
```

---

## Deprecated Endpoints (v2 → v3 migration)

| v2 endpoint | Migration path |
|---|---|
| `GET /v2/hotel/cancel-policy` | Read `options[].cancellation` from `POST /v3/hotel/detail` (Pricing) response — no separate call needed |

---

## Static Content — Static Detail API

Static (non-real-time) property metadata for a single hotel — location,
contact info, chain/brand, star rating, policies, amenities, images,
descriptions, full room-level static details. **Use for catalogue/display
content; cache up to 24 hours** — this data doesn't change with
availability/pricing.

`POST https://apitest-hms.tripjack.com/hms/v3/hotel/static-detail`

### Request
```json
{ "hid": "10000000012345" }
```

### Top-level response fields
| Field | Type | Description |
|---|---|---|
| `tjHotelId` | string | TripJack unique hotel identifier (same as `hid`) |
| `unicaId` | string | Internal content ID for dedup/cross-referencing |
| `name` | string | Hotel display name |
| `is_active` | boolean | `false` = unlisted, do not display |
| `star_rating` | string | e.g. `"4"`, `"5"` |
| `property_type` | object | `id` + `name`, e.g. `"1"` / `"Hotel"` |
| `chain` | object | `id`, `name` — e.g. `{ "id": 245, "name": "Taj" }` |
| `locale` | object | Address, coordinates, phone, fax, email |
| `policies` | object | Check-in/out times, instructions, mandatory fees, house rules |
| `amenities` | object | Map of amenity objects keyed by amenity ID |
| `images` | array | Hotel-level images |
| `descriptions` | object | Named description strings (default, amenities, dining, rooms, etc.) |
| `rooms` | object | Map of room type configurations, keyed `"0"`, `"1"`, ... |

### Locale — address & coordinates
```json
{
  "locale": {
    "address": {
      "fulladdr": "4th Floor, Tripjack Office, Gopaldas Bhawan, CP, Delhi, India, 10000001",
      "line_1": "4th Floor, Tripjack Office",
      "line_2": "Gopaldas Bhawan",
      "region": "Connaught Place, CP",
      "city": "Delhi",
      "citycode": "CTDEL",
      "statename": "Delhi",
      "regioncode": "RGNCR",
      "countryname": "India",
      "countrycode": "IN",
      "postal_code": "1000001"
    },
    "coordinates": { "lat": 10.203330479016, "long": 24.68516154266 },
    "phone": ["1-854-223-9494"],
    "fax": ["99585839302"]
  }
}
```

### Policies
```json
{
  "policies": {
    "checkInCheckOut": {
      "checkin_from": "13:00", "checkin_till": "19:30",
      "checkout_from": "06:00", "checkout_till": "12:30",
      "checkin_min_age": "18"
    },
    "instructions": "Extra-person charges may apply...",
    "special_instructions": "No front desk — contact property ahead of time.",
    "know_before_you_go": "Reservations required for spa...",
    "mandatory_fees": "Resort fee: USD 29.12 per night...",
    "optional_fees": "In-room wireless: USD 15/hr..."
  }
}
```
`mandatory_fees` (HTML) must be displayed to users before booking.
`houseRules` is a key-value map of property-specific rules.

### Amenities
```json
{
  "amenities": {
    "9": { "id": "9", "name": "Fitness facilities" },
    "2820": { "id": "2820", "name": "Indoor Pool" }
  }
}
```

### Images
```json
{
  "images": [
    {
      "caption": "Featured Image",
      "is_hero_image": true,
      "category": 3,
      "links": { "original": { "href": "https://i.travelapi.com/.../e9878546_z.jpg" } }
    }
  ]
}
```
`links` keys are pixel widths (e.g. `"70px"`, `"100px"`, `"200px"`).

### Descriptions keys
`default`, `amenities`, `dining`, `renovations`, `national_ratings`,
`business_amenities`, `rooms`, `attractions`, `location`, `headline`.

### Rooms object
Keyed by sequential integers (`"0"`, `"1"`, ...). Each entry:
```json
{
  "0": {
    "id": "224829",
    "name": "Single Room",
    "room_count": 3,
    "living_room_count": 2,
    "descriptions": { "overview": "<strong>2 Twin Beds</strong><br />269 sq ft..." },
    "amenities": {
      "130": { "id": "130", "name": "Refrigerator" },
      "1234": { "id": "1234", "name": "Warm Towels" }
    },
    "images": [{ "hero_image": true, "category": 21001, "caption": "Guestroom", "links": {} }],
    "bed_config": {
      "bed_count": 4,
      "bedroom_count": 3,
      "description": "1 King Bed, 2 Twin Beds, and 1 Sofa Bed",
      "configuration": {
        "0": { "type": "KingBed", "size": "King", "quantity": 1 },
        "1": { "type": "Twin bed", "size": "Twin", "quantity": 2 },
        "2": { "type": "Sofa Bed", "size": "sofa", "quantity": 1 }
      }
    },
    "area": { "square_meters": 20, "square_feet": 215 },
    "views": { "4146": { "id": "4146", "name": "Courtyard view" } },
    "occupancy": { "max_allowed": { "total": 5, "children": 4, "adults": 4 } }
  }
}
```
`rooms[].id` matches `roomInfo.id` in the Dynamic Detail response.

---

## Step 4 — Book API

Creates a hotel booking using the `bookingId` from Review. Two modes on the
same endpoint: **Instant Booking** (confirmed immediately, needs
`paymentInfos`) and **Hold Booking** (reserves without payment, confirmed
separately before the deadline).

`POST https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/book`

### Instant Booking request
```bash
curl --location 'https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/book' \
--header 'Content-Type: application/json' \
--header 'apikey: <your_api_key>' \
--data-raw '{
  "bookingId": "TGS208420065548",
  "roomTravellerInfo": [
    { "travellerInfo": [
        { "ti": "Mr", "pt": "ADULT", "fN": "GENIUS", "lN": "WORLD", "pan": "AAACA1111A" },
        { "ti": "Master", "pt": "CHILD", "fN": "JAPANJOT", "lN": "SINGH", "pan": "AAACA1111A" }
    ]},
    { "travellerInfo": [
        { "ti": "Mrs", "pt": "ADULT", "fN": "MANMEET", "lN": "KAUR", "pan": "BBBCB1111B" },
        { "ti": "Miss", "pt": "CHILD", "fN": "HARLEEN", "lN": "KAUR", "pan": "BBBCB1111B" }
    ]}
  ],
  "deliveryInfo": {
    "emails": ["guest@example.com"],
    "contacts": ["1234567890"],
    "code": ["+91"]
  },
  "paymentInfos": [{ "amount": 23486.76 }],
  "type": "HOTEL"
}'
```

### Hold Booking & Confirm
Omit `paymentInfos` to place a hold. The room is reserved until `ddt`
(`deadlineDatetime` from Review). Confirm before the deadline or the
booking is auto-cancelled.

```bash
# Step 1 — Hold (no paymentInfos)
curl --location 'https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/book' \
--header 'Content-Type: application/json' \
--header 'apikey: <your_api_key>' \
--data-raw '{
  "bookingId": "TGS208420065548",
  "roomTravellerInfo": [ ... ],
  "deliveryInfo": { "emails": ["guest@example.com"], "contacts": ["1234567890"], "code": ["+91"] },
  "type": "HOTEL"
}'

# Step 2 — Confirm Hold (before ddt)
curl --location 'https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/confirm-book' \
--header 'Content-Type: application/json' \
--header 'apikey: <your_api_key>' \
--data '{ "bookingId": "TJ207187962918", "paymentInfos": [{ "amount": 17577.26 }] }'
```

### GST info handling
When reseller/GST passthrough details appear in the Detail/Review response,
echo the same GST details back in the booking request under `gstInfo`:
```json
{
  "bookingId": "TGS208420065548",
  "roomTravellerInfo": [ ... ],
  "deliveryInfo": { "emails": ["test@test.com"], "contacts": ["1234567890"], "code": ["+91"] },
  "gstInfo": { "gstNumber": "29ABBCR4749R3ZF", "registeredName": "TRIPJACK" },
  "paymentInfos": [{ "amount": 23486.76 }],
  "type": "HOTEL"
}
```

### Book Response
```json
{ "bookingId": "TJ202487947162", "status": { "success": true }, "metaInfo": {} }
```

> **Async confirmation**: this response only confirms the booking *request*
> was received. Confirmation can take up to 180 seconds. Poll Booking
> Details every 5 seconds until a terminal status or 180s elapses.

### Request fields
| Field | Type | Required | Description |
|---|---|---|---|
| `bookingId` | string | Required | From Review response |
| `type` | string | Required | Must be `HOTEL` |
| `roomTravellerInfo` | array | Required | One entry per room, same order as search request |
| `roomTravellerInfo[].travellerInfo` | array | Required | One entry per guest in the room |
| `travellerInfo[].ti` | string | Required | Title: `Mr`, `Mrs`, `Ms`, `Miss`, `Master` |
| `travellerInfo[].pt` | string | Required | `ADULT` or `CHILD` |
| `travellerInfo[].fN` / `lN` | string | Required | First/last name. Lead pax name must be unique across rooms. |
| `travellerInfo[].pan` | string | Conditional | Required when `ipr` (isPanRequired) is true in Review |
| `travellerInfo[].pNum` | string | Conditional | Required when `ipm` (isPassportMandatory) is true in Review |
| `deliveryInfo.emails` | string[] | Required | Confirmation delivery |
| `deliveryInfo.contacts` | string[] | Required | Contact numbers |
| `deliveryInfo.code` | string[] | Required | Country dialing codes matching each contact |
| `paymentInfos[].amount` | double | Conditional | Include for Instant Booking; omit for Hold |

---

## Booking Management — Booking Details

Retrieves current status and full details of a booking. Poll after Book
(every 5s, up to 180s) and use for any later lookup.

`POST https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/booking-details`

```bash
curl --location 'https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/booking-details' \
--header 'Content-Type: application/json' \
--header 'apikey: <your_api_key>' \
--data '{ "bookingId": "TGS207090064619" }'
```

| Field | Type | Description |
|---|---|---|
| `bookingId` | string | From Review or Book response |

### Booking Status Values
Poll until a **terminal** status or 180s elapses (suggested interval: 5s).

| Status | Type | Description |
|---|---|---|
| `IN_PROGRESS` | Pending | Being processed by supplier |
| `PAYMENT_SUCCESS` | Pending | Payment captured; awaiting supplier confirmation |
| `PAYMENT_PENDING` | Pending | Payment not yet processed |
| `PENDING` | Pending | Generic pending state |
| `SUCCESS` | Terminal ✓ | Booking confirmed, active |
| `ON_HOLD` | Terminal ✓ | Hold confirmed — must confirm before `ddt` or auto-cancelled |
| `ABORTED` | Terminal ✗ | Failed at supplier, no charge |
| `FAILED` | Terminal ✗ | Booking request failed, no charge |
| `CANCELLATION_PENDING` | Post-Booking | Cancellation received but not processed; TripJack Ops handles offline. Poll once daily. |
| `CANCELLED` | Terminal | Successfully cancelled |

### Response shape (abbreviated — key fields)
```json
{
  "order": {
    "bookingId": "TJ2040174908940",
    "amount": 6234.55,
    "markup": 20,
    "deliveryInfo": { "emails": [], "contacts": [], "code": [] },
    "status": "ON_HOLD",
    "createdOn": "2026-05-23T11:17:29.164"
  },
  "itemInfos": {
    "HOTEL": {
      "hInfo": {
        "name": "RAMEE GUESTLINE HOTEL KHAR",
        "des": "...", "rt": 3,
        "gl": { "ln": "72.84", "lt": "19.07" },
        "ad": { "adr": "...", "postalCode": "400052", "city": { "name": "MUMBAI" }, "state": { "name": "INDIA" }, "country": { "name": "INDIA" } },
        "ops": [
          {
            "ris": [
              {
                "id": "10025299254_0", "rc": "Executive Room", "rt": "Executive Room",
                "srn": "Executive, Double", "adt": 2, "chd": 0, "cAge": [], "mb": "Breakfast",
                "ti": [{ "ti": "Mr", "pt": "ADULT", "fN": "Aryan", "lN": "Singh", "gstl": [{ "gstNumber": "27AC1Z1", "registeredName": "TRI" }] }]
              }
            ],
            "tp": 6234.55, "sc": "INR",
            "cnp": { "ifra": true, "inra": false, "pd": [ { "fdt": "2026-05-23T05:43", "tdt": "2026-11-21T00:00", "am": 0 } ] },
            "ddt": "2026-11-21T00:00",
            "ipr": false, "ipm": false, "gst_appl_amt": 315
          }
        ],
        "tjid": "100000000059",
        "checkInTime": { "minAge": 18, "endTime": "anytime", "beginTime": "2:00 PM" },
        "checkOutTime": { "beginTime": "12:00 PM" }
      },
      "query": {
        "checkinDate": "2026-11-23", "checkoutDate": "2026-11-24",
        "roomInfo": [{ "numberOfAdults": 2, "numberOfChild": 0 }],
        "searchCriteria": { "city": "614223", "countryName": "INDIA", "nationality": "106" },
        "searchPreferences": { "currency": "INR" },
        "searchId": "ui-MPHX9APK"
      }
    }
  },
  "gstInfo": { "gstNumber": "27875431", "registeredName": "TRIPJACK", "bookingId": "TJ2040174908940" },
  "currentTime": "2026-05-23T11:17:39.832",
  "hotelConfirmationNumber": "TJ2040174908940",
  "status": { "success": true, "httpStatus": 200 }
}
```

Key field reference:
- `order.status` — current booking status (see table above)
- `itemInfos.HOTEL.hInfo.ops[].cnp` — cancellation policy (`ifra`=isFreeRefundApplicable, `inra`=isNonRefundApplicable, `pd`=penalty details)
- `itemInfos.HOTEL.hInfo.ops[].ipr` / `ipm` — PAN / passport required flags
- `itemInfos.HOTEL.hInfo.ops[].ddt` — free cancellation deadline
- `itemInfos.HOTEL.query.searchId` — the originating search session

---

## Booking Management — Booking List

Retrieves hotel bookings created within a date range: status, price, room
details, traveller info, cancellation policy for each.

`POST https://apitest-hotel-booker.tripjack.com/oms/v1/hotel/bookings`

```bash
curl --location 'https://apitest-hotel-booker.tripjack.com/oms/v1/hotel/bookings' \
  --header 'Content-Type: application/json' \
  --header 'apikey: <your_api_key>' \
  --data '{
  "startDate": "2026-08-14T00:00:00",
  "endDate": "2026-08-19T13:59:59"
}'
```

| Field | Type | Description |
|---|---|---|
| `startDate` / `endDate` | string | Booking search date range |

### Response (abbreviated)
```json
{
  "status": { "success": true },
  "bookings": [
    {
      "bookingId": "TJ208502829712",
      "status": "CANCELLED",
      "totalPrice": 2240.93,
      "options": [
        {
          "totalPrice": 2240.93,
          "cancellationPolicy": {
            "ifra": true, "inra": false,
            "pd": [
              { "fdt": "2026-08-14T12:48:00", "tdt": "2027-02-11T23:59:00", "am": 0.0 },
              { "fdt": "2027-02-11T23:59:00", "tdt": "2027-02-15T00:00:00", "am": 2217.33 }
            ]
          },
          "rooms": [
            {
              "totalPrice": 2465.02, "mealBasis": "Breakfast",
              "checkInDate": "2027-02-14", "checkOutDate": "2027-02-15",
              "numberOfAdults": 2, "numberOfChild": 0, "childAges": [],
              "travellerInfo": [
                { "ti": "Mr", "fN": "Anjali", "lN": "Verma", "pt": "ADULT" },
                { "ti": "Mr", "fN": "Michael", "lN": "Verma", "pt": "ADULT" }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

---

## Booking Management — Booking Cancellation

Cancels a confirmed booking. `bookingId` passed as URL path parameter.
Applicable cancellation charges from the policy apply.

`POST https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/cancel-booking/{bookingId}`

```bash
curl --location --request POST \
  'https://apitest-hotel-booker.tripjack.com/oms/v3/hotel/cancel-booking/TJS20990000003651' \
  --header 'apikey: <your_api_key>'
```

No request body required — `bookingId` is embedded in the URL path.

### Success response
```json
{ "status": { "success": true } }
```

> Retrieve final cancellation status via the Booking Details API (same one
> used post-Book).
>
> **CANCELLATION_PENDING**: if the booking moves to this status, TripJack
> Ops processes the cancellation offline with the supplier. Poll Booking
> Details **once per day** until it updates to `CANCELLED`.

---

## Static Content — Nationalities

Full list of supported nationalities with country codes, dial codes, ISO
codes. Use to populate nationality dropdowns.

`GET https://apitest.tripjack.com/hms/v3/nationality-info`

```bash
curl --location 'https://apitest.tripjack.com/hms/v3/nationality-info' \
  --header 'apikey: <your_api_key>'
```

### Response
```json
{
  "nationalityInfos": [
    { "countryName": "India", "name": "India", "dialCode": "91", "countryId": "106", "code": "IN" }
  ]
}
```

---

## Static Content — Fetch Static Hotels

Bulk static hotel data, for local catalogue sync. **Never use for pricing
or other dynamic booking data** — use dynamic Detail for that.

`POST https://apitest.tripjack.com/hms/v3/fetch-static-hotels`

Three request modes:
```bash
# Type 1 — Fetch first page of all hotels
curl --location 'https://apitest.tripjack.com/hms/v3/fetch-static-hotels' \
  --header 'Content-Type: application/json' --header 'apikey: <your_api_key>' \
  --data '{}'

# Type 2 — Fetch next page (pass 'next' token from previous response)
curl --location 'https://apitest.tripjack.com/hms/v3/fetch-static-hotels' \
  --header 'Content-Type: application/json' --header 'apikey: <your_api_key>' \
  --data '{ "next": "MTAwIDM2MjM4" }'

# Type 3 — Sync updates since a specific datetime (incremental sync)
curl --location 'https://apitest.tripjack.com/hms/v3/fetch-static-hotels' \
  --header 'Content-Type: application/json' --header 'apikey: <your_api_key>' \
  --data '{ "lastUpdateTime": "2024-03-08T16:42" }'
```

| Field | Type | Description |
|---|---|---|
| `lastUpdateTime` | string (ISO 8601) | Only hotels updated after this timestamp (Type 3, incremental sync) |
| `next` | string | Pagination cursor from previous response (Type 2) |

### Response
```json
{
  "hotelOpInfos": [
    {
      "tjHotelId": "39591724",
      "unicaId": "10004566",
      "name": "Hotel Nele",
      "description": "{\"amenities\":\"Pamper yourself with a visit...\"}",
      "rating": 3,
      "isDeleted": false,
      "geolocation": { "ln": "11.57229..." }
    }
  ]
}
```

> For incremental syncs, use `lastUpdateTime` (Type 3) to fetch only new or
> changed hotels since your last sync.

---

## Static Content — Deleted Hotels (Deprecated)

> ⚠️ **Deprecated.** Prefer the V3 New Static Content APIs below for
> catalogue sync. Do not use for pricing or dynamic booking data.

Returns hotel IDs de-listed since a given timestamp — remove from local
catalogue.

`POST https://apitest.tripjack.com/hms/v3/fetch-static-hotels/deleted`

```bash
# Type 1 — First page
curl --location 'https://apitest.tripjack.com/hms/v3/fetch-static-hotels/deleted' \
  --header 'Content-Type: application/json' --header 'apikey: <your_api_key>' \
  --data '{ "lastUpdateTime": "2024-02-21T12:42" }'

# Type 2 — Next page
curl --location 'https://apitest.tripjack.com/hms/v3/fetch-static-hotels/deleted' \
  --header 'Content-Type: application/json' --header 'apikey: <your_api_key>' \
  --data '{ "next": "<cursor>" }'
```

| Field | Type | Description |
|---|---|---|
| `hotelOpInfos[].tjHotelId` | string | Deleted hotel ID — remove from local catalogue |
| `next` | string | Pagination cursor for next page; absent when no more results |

---

## V3 New Static Content APIs

All new in v3: hotel-to-ID mapping, full hotel content retrieval, country
listings, city/region ID lookups, incremental mapping sync. All use `apikey`
header auth, served from a cacheable static data layer. Base:
`apitest-hms.tripjack.com` (test).

### API Index
| Method | Endpoint | Purpose |
|---|---|---|
| POST | Hotel ID Mapping | Map `tjHotelId` by country or region |
| POST | Hotel Static Content | Full property & room static content by hotel IDs |
| GET | Fetch Countries | List supported countries |
| GET | City Region IDs | City/region ID lookup |
| POST | Hotel Mapping Sync | Incremental mapping sync |

### Hotel ID Mapping

`POST https://apitest-hms.tripjack.com/hms/v3/content/fetch-hotel-mapping`

```json
{
  "countryName": "UNITED ARAB EMIRATES",
  "regionIds": ["740217"],
  "page": 0,
  "size": 2000
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `countryName` | string | Conditional | Full country name, uppercase. Required if `regionIds` not provided. |
| `regionIds` | string[] | Conditional | Region IDs to filter. Required if `countryName` not provided. |
| `page` | integer | Required | Zero-indexed page number |
| `size` | integer | Required | Records per page, max 2000 |

Response:
```json
{
  "status": { "success": true, "httpStatus": 200 },
  "hotels": [
    { "tjHotelId": "100001728661", "unicaId": "71354426" },
    { "tjHotelId": "100002113577", "unicaId": "72332032" }
  ],
  "pageable": {
    "pageNumber": 0, "pageSize": 2000, "offset": 0,
    "totalElements": 8000, "totalPages": 4, "size": 2000
  }
}
```

### Hotel Static Content

`POST https://apitest-hms.tripjack.com/hms/v3/content/fetch-hotel-content`

> ⚠️ Max `hotelIds` per request is **100**. Above that: 400 error
> `"Max hotel ids size passed in request should be 100"`.

```json
{ "hotelIds": ["100001743803", "100001728661"] }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `hotelIds` | string[] | Required | TripJack hotel IDs, max 100 |

Response includes full static content per hotel: `locale`, `policies`,
`amenities`, `images`, `descriptions` (same shapes as Static Detail).
Sample locale/amenity/image excerpt:
```json
{
  "hotels": [
    {
      "locale": {
        "address": {
          "fulladdr": "15 Sheikh Mohammed bin Rashid Blvd, Downtown Dubai, UAE, 00000",
          "city": "Dubai", "citycode": "2994", "statename": "Dubai",
          "regioncode": "697010", "countryname": "United Arab Emirates",
          "countrycode": "AE", "postal_code": "00000"
        },
        "coordinates": { "lat": 25.19403, "long": 55.269318 }
      },
      "policies": { "know_before_you_go": "{\"Children Policy\":\"Age between 2 to 12 is considered children.\"}" },
      "amenities": { "0": { "id": "10008", "name": "Air Conditioner" } },
      "images": [{ "caption": "CoverImage", "is_hero_image": true, "links": { "Standard": { "href": "https://..." } } }],
      "descriptions": { "headline": "Experience..." }
    }
  ]
}
```

### Fetch Countries

`GET https://apitest-hms.tripjack.com/hms/v3/content/fetch-countries`

```bash
curl --location 'https://apitest-hms.tripjack.com/hms/v3/content/fetch-countries' \
  --header 'apikey: <your_api_key>'
```

Response:
```json
{
  "status": { "success": true, "httpStatus": 200 },
  "hotelCountries": ["AFGHANISTAN", "ALBANIA", "UNITED ARAB EMIRATES", "UNITED KINGDOM", "INDIA"]
}
```
(200+ countries total)

### City Region IDs

`GET https://apitest-hms.tripjack.com/hms/v3/content/...` (query params, incl. `countryName` and pagination `cursor`)

Response:
```json
{
  "status": { "success": true, "httpStatus": 200 },
  "hotelCityRegionIds": [
    {
      "cityName": "ROXIE", "cityRegionId": 113466, "regionName": "ROXIE",
      "countryName": "UNITED STATES", "regionType": "CITY",
      "fullRegionName": "ROXIE, MISSISSIPPI, UNITED STATES OF AMERICA"
    }
  ],
  "nextCursor": "<base64-cursor>",
  "hasMore": true
}
```

| Field | Type | Description |
|---|---|---|
| `hotelCityRegionIds[].cityRegionId` | integer | Pass as a value in `regionIds` for Hotel Mapping endpoint |
| `hotelCityRegionIds[].regionType` | string | Always `"CITY"` for this endpoint |
| `nextCursor` | string | Base64 cursor for next page; pass as `cursor` query param |
| `hasMore` | boolean | true if more pages available |

### Hotel Mapping Sync API

Incremental sync of hotel mappings (new/updated records).

`POST .../fetch-hotel-mapping-sync?page={number}` (page param optional, for UI display)

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | Required | `NEW` or `UPDATE` |
| `lastUpdateTime` | ISO 8601 datetime | Required | Filter for records updated after this time |
| `cursor` | string | Optional | From previous response's `nextCursor` |

```bash
# First page
curl -X POST https://<HOST>/hms/v3/content/fetch-hotel-mapping-sync \
  -H "Content-Type: application/json" -H "apikey: <your_api_key>" \
  -d '{ "type": "NEW", "lastUpdateTime": "2024-01-01T00:00:00Z" }'

# Next page (with cursor)
curl -X POST https://<HOST>/hms/v3/content/fetch-hotel-mapping-sync \
  -H "Content-Type: application/json" -H "apikey: <your_api_key>" \
  -d '{ "type": "NEW", "lastUpdateTime": "2024-01-01T00:00:00Z", "cursor": "MTcwNDA2NzIwMDAwMCxIT1RFTF8xMjM=" }'
```

Response:
```json
{
  "hotels": [{ "tjHotelId": "TJ123" }, { "tjHotelId": "TJ456" }],
  "pageable": { "pageNumber": 0, "pageSize": 2000, "totalElements": 8000, "totalPages": 4 },
  "nextCursor": "MTcwNDA2NzIwMDAwMCxIT1RFTF8xMjM=",
  "status": { "success": true, "httpStatus": 200 }
}
```

Known error: `type=DELETE` passed to the active mapping sync API → invalid
(returns error; not a supported type for this endpoint). `500 Internal
Server Error` on unexpected server-side errors.

---

## Error Codes

All v3 APIs return a standardized error envelope. Supplier-internal info is
never exposed.

```json
{
  "status": { "success": false },
  "error": {
    "code": "INVALID_HOTEL_ID",
    "message": "The provided hotelId does not exist or is inactive.",
    "requestId": "1p6IYhwDQ9NGwiZ8FaigQz"
  }
}
```

### Named error codes
| Code | HTTP | Applies to | Description |
|---|---|---|---|
| `INVALID_HOTEL_ID` | 400 | Detail, Review | `tjHotelId` not recognised or hotel inactive |
| `INVALID_SEARCH_ID` | 400 | Detail, Review | `searchId` doesn't match an active session |
| `SEARCH_SESSION_EXPIRED` | 410 | Detail, Review | Session expired (~15 min). Re-run Search; don't retry with same `searchId`. |
| `OPTION_SOLD_OUT` | 409 | Review | Option no longer available. Return to Detail, choose another. Don't call Book. |
| `INVALID_DATE_RANGE` | 400 | Search, Detail | `checkIn` in the past, or `checkOut` not after `checkIn` |
| `INVALID_ROOM_CONFIG` | 400 | All | 0 adults, too many rooms, or empty rooms array |
| `SUPPLIER_UNAVAILABLE` | 503 | Detail, Review | Upstream supplier down. Retry with backoff: 1s→2s→4s, max 3. |
| `UNAUTHORIZED` | 401 | All | Missing/invalid token |
| `RATE_LIMITED` | 429 | All | Check `Retry-After` header for backoff duration |

### Numeric error code reference
| Code | Type | Message | HTTP |
|---|---|---|---|
| 6502 | SEARCH_SESSION_EXPIRED | Session expired (~15 min). Re-run Search. Don't retry same searchId. | 200 |
| 6503 | HOTEL_ID_MISMATCH_WITH_SESSION | hotelId {hotelId} not in result set for searchId {searchId} | 200 |
| 6504 | INVALID_HOTEL_ID | hotelId doesn't exist or is inactive | 200 |
| 6505 | OPTION_ID_NOT_FOUND | optionId {optionId} not found in the referenced Detail response | 200 |
| 6506 | OPTION_SOLD_OUT | Option no longer available. Return to Detail, choose another. Don't call Book. | 200 |
| 6507 | GUEST_INFO_INCOMPLETE | Required guest field(s) missing for traveller {index} | 200 |
| 6508 | GUEST_NAME_INVALID | Guest name blank or has unsupported characters for traveller {index} | 200 |
| 6509 | TRAVELLER_COUNT_MISMATCH | {provided} traveller(s) supplied, {expected} required | 200 |
| 6510 | DUPLICATE_BOOKING_REQUEST | A booking with this reference is already processing/completed | 200 |
| 6511 | BOOKING_NOT_FOUND | No booking found for bookingId {bookingId} | 200 |
| 6512 | CANCELLATION_NOT_ALLOWED | Booking outside cancellable window or non-refundable | 200 |
| 6513 | CANCELLATION_ALREADY_IN_PROGRESS | Cancellation for bookingId {bookingId} already pending | 200 |
| 6514 | SUPPLIER_UNAVAILABLE | Option not available | 200 |
| 6515 | SUPPLIER_TIMEOUT | Option sold out | 200 |
| 6516 | INTERNAL_ERROR | Unexpected internal error, logged; retry with standard backoff | 200 |
| 6517 | RATE_LIMITED | Check Retry-After header | 429 |
| 6518 | UNAUTHORIZED | Missing/invalid auth token | 401 |
| 6519 | FORBIDDEN | API key not authorized for requested product/resource | 403 |
| 6520 | API_KEY_SUSPENDED | Key suspended — contact TripJack account manager | 403 |
| 6521 | INVALID_REQUEST_FORMAT | Body not valid JSON or doesn't match schema | 400 |
| 6522 | INVALID_DATE_RANGE | checkIn in past, or checkOut not after checkIn | 400 |
| 6523 | CHECKIN_DATE_IN_PAST | checkIn ({value}) earlier than today | 400 |
| 6524 | CHECKOUT_BEFORE_CHECKIN | checkOut ({value}) must be after checkIn ({checkIn}) | 400 |
| 6525 | DATE_RANGE_TOO_FAR_OUT | checkIn beyond max advance-booking window ({maxDays} days) | 400 |
| 6526 | LENGTH_OF_STAY_TOO_LONG | Stay ({nights} nights) exceeds max ({maxNights}) | 400 |
| 6527 | INVALID_ROOM_CONFIG | Empty rooms array, or a room has 0 adults | 400 |
| 6528 | CHILD_AGE_REQUIRED | Room {roomIndex} has {children} child(ren) but no ages provided | 400 |
| 6529 | CHILD_AGE_COUNT_MISMATCH | Room {roomIndex}: {children} declared vs {providedCount} ages given | 400 |
| 6530 | CHILD_AGE_OUT_OF_RANGE | childAge {value} in room {roomIndex} outside 0–17 | 400 |
| 6531 | CHILD_AGE_INVALID_FORMAT | childAge value in room {roomIndex} not a valid whole number | 400 |
| 6532 | ADULTS_PER_ROOM_EXCEEDS_MAXIMUM | Room {roomIndex} has {adults} adults, exceeds max of 9 | 400 |
| 6533 | CURRENCY_NOT_SUPPORTED | Currency {currency} not supported for this account | 400 |
| 6534 | NATIONALITY_RESTRICTED | Nationality {nationality} restricted for this hotel/country | 403 |
| 6535 | PRICE_CHANGED | Price changed since fetched. Re-confirm with user before proceeding. | 200 |
| 6536 | ORDER_INVALID_ACTION | Requested action not valid for current order state | 200 |
| 6537 | HOLD_NOT_ALLOWED | Hold booking not allowed for this option | 200 |
| 6538 | DUPLICATE_ORDER_CREDIT_LINE_OR_WALLET | Duplicate booking — existing booking via Credit Line/Wallet already made | 200 |
| 6539 | INSUFFICIENT_BALANCE | Insufficient balance to complete booking | 200 |
| 6540 | HOLD_PAYMENT_NOT_ALLOWED | Hold bookings must not include payment info | 200 |

### Retry strategy
| Error | Retry? | Strategy |
|---|---|---|
| `SUPPLIER_UNAVAILABLE` (503) | Yes | Exponential backoff — 1s, 2s, 4s. Max 3 retries. |
| `RATE_LIMITED` (429) | Yes | Wait for `Retry-After` header duration |
| `OPTION_SOLD_OUT` (409) | No | Requires user action — show alternate options |
| `SEARCH_SESSION_EXPIRED` (410) | No | Re-initiate full search flow |

---

## SLA & Performance

| API | P95 target |
|---|---|
| Search API | < 3s |
| Detail API | < 5s (real-time supplier call) |
| Review API | < 3s (re-validates known option) |

> Static hotel data (name, images, amenities) served from cacheable layer —
> 100 hotels must return within 5 seconds. Dynamic data (pricing,
> availability) is fetched real-time from suppliers.

---

## Changelog

| Version | Date | Changes |
|---|---|---|
| v3.0 (Current) | Aug 2026 | Added Booking List endpoint (`POST /oms/v1/hotel/bookings`) to retrieve hotel bookings by date range — status, price, rooms, travellers, cancellation policy. |
| v3.0 | Feb 2026 | Initial v3 release. New Search response structure — Detail now returns dynamic-only data with embedded cancellation policy, `apikey` auth, `correlationId` requirement, and the rest of the breaking changes in the table at the top of this doc. |

---

## Official UAT / Certification Test Cases

TripJack's own required test-case matrix, completed before go-live:

| # | Description | Search type | Adults | Children | Rooms |
|---|---|---|---|---|---|
| 1 | Auto-cancellation of booking in ON_HOLD status (unconfirmed case) | Domestic | 1 | 0 | 1 |
| 2 | Instant booking | Domestic | 4 | 2 | 1 |
| 3 | Hold booking then confirm booking | Domestic | 2, 3, 2 | 2, 1, 1 | 3 |
| 4 | Create a booking within cancellation deadline, cancel it | Domestic | 1, 2, 1, 1, 1 | 2, 1, 1, 0, 0 | 5 |
| 5 | Create a booking outside cancellation deadline, cancel it | International | 4 | 2 | 1 |
| 6 | Cancel booking in ON_HOLD status (without confirming it) | International | 2, 3, 2 | 2, 1, 2001* | 3 |
| 7 | Instant booking | International | 1, 2, 1, 1, 1 | 2, 1, 1, 0, 0 | 5 |
| 8 | Instant booking | International | 3, 3 | 0, 0 | 2 |
| 9 | Verify cancellation rules/policies are displayed on your application | — | — | — | — |
| 10 | Same PAN for all rooms — create booking using same PAN across all rooms (Indian nationals only) | Domestic/International | 2, 2 | 1, 0 | 2 |
| 11 | Same PAN for all guests — create booking using same PAN for all guests | — | — | — | — |

\* Value as captured from source; verify against your sandbox before relying on it — likely a documentation artifact.

> These are the specific scenarios TripJack expects to see completed when
> client integration is done. Use this table as the baseline for your QA
> test plan before requesting production credentials.

---

## Quick host/endpoint reference

| Purpose | Host | Path |
|---|---|---|
| Listing | `apitest-hms.tripjack.com` | `/hms/v3/hotel/listing` |
| Detail / Pricing | `apitest-hms.tripjack.com` | `/hms/v3/hotel/pricing` |
| Review | `apitest-hms.tripjack.com` | `/hms/v3/hotel/review` |
| Static Detail | `apitest-hms.tripjack.com` | `/hms/v3/hotel/static-detail` |
| Fetch Static Hotels | `apitest.tripjack.com` | `/hms/v3/fetch-static-hotels` |
| Fetch Deleted Hotels (deprecated) | `apitest.tripjack.com` | `/hms/v3/fetch-static-hotels/deleted` |
| Hotel ID Mapping | `apitest-hms.tripjack.com` | `/hms/v3/content/fetch-hotel-mapping` |
| Hotel Static Content | `apitest-hms.tripjack.com` | `/hms/v3/content/fetch-hotel-content` |
| Fetch Countries | `apitest-hms.tripjack.com` | `/hms/v3/content/fetch-countries` |
| Hotel Mapping Sync | `apitest-hms.tripjack.com` | `/hms/v3/content/fetch-hotel-mapping-sync` |
| Nationalities | `apitest.tripjack.com` | `/hms/v3/nationality-info` |
| Book | `apitest-hotel-booker.tripjack.com` | `/oms/v3/hotel/book` |
| Confirm Hold | `apitest-hotel-booker.tripjack.com` | `/oms/v3/hotel/confirm-book` |
| Booking Details | `apitest-hotel-booker.tripjack.com` | `/oms/v3/hotel/booking-details` |
| Booking List | `apitest-hotel-booker.tripjack.com` | `/oms/v1/hotel/bookings` |
| Cancel Booking | `apitest-hotel-booker.tripjack.com` | `/oms/v3/hotel/cancel-booking/{bookingId}` |

> **Note**: production hosts will differ from these `apitest-*` test hosts —
> confirm production URLs with TripJack once you have live credentials.
