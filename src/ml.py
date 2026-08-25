from pathlib import Path

import joblib

MODEL_PATH = Path(__file__).resolve().parent / "artifacts" / "severity_model.pkl"

_bundle = None


def _load():
    global _bundle
    if _bundle is None:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                "No trained model found. Run: python scripts/generate_synthetic_data.py "
                "&& python scripts/train_model.py"
            )
        _bundle = joblib.load(MODEL_PATH)
    return _bundle


def predict_severity(area: str, incident_type: str, timestamp: str) -> str:
    bundle = _load()
    model, le_area, le_type, le_severity = (
        bundle["model"], bundle["le_area"], bundle["le_type"], bundle["le_severity"]
    )

    area = area.strip().title()
    incident_type = incident_type.strip().lower()

    try:
        hour = int(timestamp.split(" ")[-1].split(":")[0])
    except (IndexError, ValueError):
        hour = 12

    try:
        area_encoded = le_area.transform([area])[0]
        type_encoded = le_type.transform([incident_type])[0]
    except ValueError:
        return "Unknown"

    prediction = model.predict([[area_encoded, type_encoded, hour]])
    return le_severity.inverse_transform(prediction)[0]


def known_areas() -> list[str]:
    return list(_load()["le_area"].classes_)


def known_types() -> list[str]:
    return list(_load()["le_type"].classes_)
