# Emergency Intelligence System

Multi-hazard emergency intelligence for Kenya — crime, flood, fire, and
medical incidents, reported by citizens and officers, scored for risk, and
shown on a live map. Built by [Triagia](https://github.com/gateri001).

See `docs/architecture.md` for how it fits together and `docs/privacy_policy.md`
for how data is handled.

## Run it locally

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt

# generate the synthetic incident dataset the risk surface reads from
python scripts/generate_synthetic_data.py

# optional: real open data (GDACS is instant; UNOSAT/FIRMS download data once)
python scripts/ingest_gdacs.py
python scripts/ingest_unosat_flood.py
python scripts/ingest_firms.py

# create an officer login (no default credentials ship in source)
python scripts/create_officer.py <your-username>

# set a real JWT signing secret - without this, a random one is generated
# per process start and officer sessions won't survive a restart. Never
# commit the real value.
set EIS_SECRET_KEY=<32+ random bytes>          # Windows cmd
$env:EIS_SECRET_KEY = "<32+ random bytes>"     # PowerShell

# run the API
uvicorn src.main:app --reload
```

Then open:
- `http://127.0.0.1:8000/docs` — interactive API docs
- `http://127.0.0.1:8000/dashboard/` — the live dashboard

### Real SMS (optional)

Broadcasts default to a console stub that only logs what would be sent.

**Scrappy/no-budget default:** set `BROADCAST_PROVIDER=smsgate` plus
`SMS_GATE_URL`, `SMS_GATE_USERNAME`, `SMS_GATE_PASSWORD` to send through a
self-hosted [SMS Gate](https://sms-gate.app) instance - a spare Android
phone with a real SIM acting as your own SMS gateway. Open source (Apache
2.0), no aggregator markup, no per-message API fee - the only real cost is
your own carrier's SMS/bundle rate. Set it up in the app's "Private Mode"
so no third party ever sees message content.

**Once there's funding:** set `BROADCAST_PROVIDER=africastalking` plus
`AT_USERNAME` and `AT_API_KEY` from an
[Africa's Talking](https://account.africastalking.com/) account for
aggregator-grade delivery guarantees at real scale.

Both code paths are implemented but have not been exercised against a live
account/device yet - treat the first real send through either as a test.

### Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v

# dependency vulnerability scan
pip-audit -r requirements.txt
```

## Status

Early — one unified backend, a baseline risk model on synthetic data, and a
working dashboard. Real (non-synthetic) data sourcing and route/safe-zone
optimization are next. See `docs/architecture.md` for what's explicitly not
built yet.
