# How the Emergency Intelligence System works

Written to be readable by someone who has never programmed, then to get more
technical as you go down. Every claim here is taken from the code as it is now.
Where something is not built, it says so.

---

## 1. What it is, in one breath

Anyone in Kenya can report something dangerous - a robbery, a flood, a fire, an
accident, a missing child - by tapping a spot on a map. The system decides how
much to trust the report, works out how risky that area is, and puts the
important ones in front of a human officer, who decides whether to warn people
nearby by SMS. It also pulls in real satellite and disaster data, so it is not
only relying on what people type.

The one-sentence version for someone who asks "what do you do?":

> We turn scattered reports into a live risk map, and get the right warning to
> the right people nearby fast enough to matter - with a human deciding before
> anyone is texted.

---

## 2. A report's journey (the most useful thing to understand)

Follow one report - say, "flood near Githurai" - from the moment it is sent.

1. **Someone taps the map and submits.** The dashboard sends the type, the exact
   location, an optional description, and the time. It does *not* send a name or
   a phone number - none is collected. That is on purpose (see section 5).
2. **The door check.** The system refuses nonsense: a location outside Kenya, a
   made-up type, a garbled time, a wall of text. (`src/schemas.py`)
3. **The instant reaction (the "reflex" layer).** In a few milliseconds it gives
   the report a first trust score from things it knows immediately: who sent it
   (an officer's report starts far more trusted than an anonymous one), whether
   the sender says they have a photo, and - for floods - whether the spot is
   inside an area that satellite imagery proved flooded before. The sender gets
   "received" straight away. (`src/reflex.py`)
4. **The careful follow-up (the "strategic" layer).** A moment later, in the
   background, it does the slower work: are other people reporting the same
   thing nearby, at about the same time? How risky is this place given
   everything that has happened around it? It then updates the report's final
   trust score. (`src/strategic.py`)
5. **Everyone else can weigh in.** Anyone can press Confirm or Dispute on a
   report. That nudges its trust score up or down. No name is attached to a vote
   either.
6. **It lands in one of three tiers.**
   - *in app* - shows on the map and feed, nobody is texted;
   - *SMS recommended* - shows up on an officer's "recommended" list;
   - *critical* - same list, at the top.
7. **A human decides.** An officer looks at the recommended list, sees exactly
   what message would go out and how many people it would reach, and presses
   send - or doesn't. **The system never sends a mass SMS by itself.**
   (`/alert/broadcast`)

Why the reflex/strategic split? The careful work gets slower as more data
accumulates. Doing it *before* replying would make reporting slower exactly when
many people report at once (a flood). So the reply is instant and the deep work
follows. This idea was tested first in a separate experiment (a small game
environment) before being used here.

---

## 3. The three ideas that make it different

### 3a. Risk is a smooth surface, not a list of neighbourhoods
An earlier version scored risk for 16 hard-coded area names, which meant it
could not answer for anywhere else. Now, every past incident casts a "glow" on
the map - stronger the closer you are and the more recent it was, fading with
distance and with time (a 30-day half-life). Add the glows up and you get a
risk value for *any* point in Kenya. (`src/risk_surface.py`)

The map's "Risk surface" toggle draws exactly this. Important honesty: the
baseline data behind it is **synthetic** (generated, centred on Nairobi), so
today it shows the model working, not real-world risk. It moves as real reports
arrive.

### 3b. Trust depends on what is being reported
A fabricated flood report gains the faker nothing, so a single flood report
starts fairly trusted. An accusation of crime can harm the person accused if it
is false, so a single crime report starts more sceptical and needs more
corroboration before the system leans on it. Numbers live in
`src/confidence.py`.

### 3c. Recommend, don't send
Scores and tiers only *rank* what a human should look at. Every actual SMS is a
person's decision, logged with who pressed the button.

---

## 4. Where real data comes from

| Source | What it gives | How it is used |
|---|---|---|
| NASA FIRMS (satellite) | Fires detected in the last 24 hours | Shown as fires, ranked by their radiative power so a big fire outranks a small farm burn |
| UNOSAT (UN satellite mapping) | Exactly which areas flooded in April 2024 | Drawn on the map; a new flood report inside one of those areas is trusted more |
| GDACS (disaster alerts) | National-scale flood/drought alerts | Shown in the Data tab |
| Healthsites (OpenStreetMap-based) | ~960 real hospitals and clinics | "Nearest medical help" - for a medical emergency the useful question is "where is help?", not "where is safe?" |

A live flood feed from satellite radar (Copernicus) was researched but needs a
free account signup that only a person can do; it is not built.

---

## 5. Safety rules (the parts most worth explaining to a sceptic)

- **No reporter identity, ever, on normal reports.** Because none is stored,
  none can leak. A witness who reports a crime can choose "officers only": the
  report never appears in the public feed, and even officers do not see who
  sent it.
- **"Officers only" cannot be bypassed.** An earlier version leaked hidden
  reports through the vote button; that was found and fixed, with a test.
- **Missing Child Alert plays by opposite rules on purpose.** Every report
  starts *hidden*; an officer must verify it before anyone can see it. Why: a
  false report (for example in a custody dispute) does real harm and involves a
  child's details. It is also the one place a reporter's phone number *is*
  collected - because it is an investigation and police must be able to reach
  the person - and that number is visible only to officers and can never be put
  into a broadcast message (the system refuses).
- **A missing child found safe is shown; one found deceased or a false report
  is never shown publicly** - those need a human, not an automatic feed.
- **The dashboard escapes everything it displays.** A security hole where a
  crafted report could run code in a viewer's browser was found and fixed.

---

## 6. What each file does

| File | In plain words |
|---|---|
| `src/main.py` | The front desk: every web address the system answers, and the rules for who may use each |
| `src/schemas.py` | The door check: what a valid report, phone number or location looks like |
| `src/reflex.py` | The instant first reaction to a report |
| `src/strategic.py` | The slower, careful follow-up in the background |
| `src/confidence.py` | How much to trust a report, and which tier it lands in |
| `src/risk_surface.py` | The "glow" model: risk at any point, and the map layer |
| `src/routing.py` | "Route me somewhere lower-risk nearby" (a grid search, not street directions) |
| `src/broadcast.py` | Sending SMS: picks who is nearby (once each), sends, counts what really went out. Can be a pretend sender, Africa's Talking, or your own phone as a gateway |
| `src/database.py` | The tables where everything is stored |
| `src/auth.py` | Officer login |
| `src/geo.py` | Distance between two points on Earth |
| `static/index.html` | The whole dashboard (map, forms, officer console) |
| `scripts/ingest_*.py` | Pull in the real data above |
| `scripts/create_officer.py` | Make an officer login |
| `tests/` | ~48 automatic checks, including ones for the security fixes |
| `docs/architecture.md` | The technical reasoning behind each decision |
| `docs/privacy_policy.md` | What data is held and why |

---

## 7. The questions you will be asked - honest one-minute answers

**"How do you stop false alarms?"**
A report alone never texts anyone. It gets a trust score; sceptical categories
(crime) need corroboration; other people can dispute it; and a human officer
sees the exact message and audience before anything is sent. What we cannot yet
claim is a measured false-alarm rate - there is no real-world data to measure
it on.

**"How is this different from a WhatsApp group?"**
Groups depend on people choosing to forward a message. Here the audience is
chosen by *where you are*, not who saw a post, and it ranks and cross-checks
reports instead of amplifying whatever is loudest. Honest gap: it currently
reaches only people who signed up for alerts; true no-signup reach (cell
broadcast) needs a telecom partnership.

**"What if it's wrong and someone is hurt?"**
Every broadcast is logged with who sent it, what it said, and how many it
reached, and a person made that call. The tiers are advice, not orders. Legal
liability is a real open question we have not resolved and need proper advice
on.

**"Why would police trust an app they don't control?"**
Officers are the decision-makers inside it, not the audience: they verify
missing-child cases, see officers-only reports, and press send. Whether a
police service would actually adopt it is a relationship question, not a coding
one, and we have not had that conversation yet.

**"What stops it being used to spread panic or target someone?"**
Rate limits on public endpoints, input checks, no mass SMS without a logged
officer, and missing-child reports invisible until verified. Rate limiting is
per network address, which is a weak signal - determined abuse is not fully
solved.

**"How do you make money?"** Not decided, and this document will not invent an
answer. (Earlier thinking was that free-to-citizens is likely right and revenue
would come from institutions.)

---

## 8. What is NOT built (so you never overclaim)

- **Real SMS has never been sent.** The sending code exists for two providers
  but has not been tested against a real phone or account.
- **No real (non-synthetic) incident history.** The risk surface's baseline is
  generated.
- **Routing is a grid search, not street directions**, and "safe" means
  "lower-risk nearby", not a verified shelter.
- **No deployment.** It runs on one computer; there is no public website yet.
- **No photo/video upload.** "I have a photo" is a checkbox that nudges trust
  slightly; nothing is stored.
- **No roles beyond citizen and officer**, no audit log, no data-retention
  policy yet (the privacy policy lists these as planned).
- **The urgency thresholds for satellite fires (25 and 100 MW) are judgement
  calls**, not validated.
- **Full-screen "unmissable" alerts** need a native phone app (possible on
  Android, not on iPhone through a normal app) and do not exist yet.

---

## 9. Try it

```
python scripts/create_officer.py <name>       # once
uvicorn src.main:app --reload
# open http://127.0.0.1:8000/dashboard/
pytest tests/ -q
```
