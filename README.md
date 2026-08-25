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

# generate training data + train the severity model
python scripts/generate_synthetic_data.py
python scripts/train_model.py

# create an officer login (no default credentials ship in source)
python scripts/create_officer.py <your-username>

# run the API
uvicorn src.main:app --reload
```

Then open:
- `http://127.0.0.1:8000/docs` — interactive API docs
- `http://127.0.0.1:8000/dashboard/` — the live dashboard

## Status

Early — one unified backend, a baseline risk model on synthetic data, and a
working dashboard. Real (non-synthetic) data sourcing and route/safe-zone
optimization are next. See `docs/architecture.md` for what's explicitly not
built yet.
