"""
Trains the severity-prediction model on the synthetic dataset.
Run: python scripts/generate_synthetic_data.py && python scripts/train_model.py
"""
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import LabelEncoder

ROOT = Path(__file__).resolve().parent.parent


def main():
    df = pd.read_csv(ROOT / "data" / "synthetic_incidents.csv")
    df["hour"] = df["time"].str.split(":").str[0].astype(int)

    le_area = LabelEncoder()
    le_type = LabelEncoder()
    le_severity = LabelEncoder()

    df["area_encoded"] = le_area.fit_transform(df["area"])
    df["type_encoded"] = le_type.fit_transform(df["type"])
    df["severity_encoded"] = le_severity.fit_transform(df["severity"])

    X = df[["area_encoded", "type_encoded", "hour"]]
    y = df["severity_encoded"]

    model = RandomForestClassifier(n_estimators=200, random_state=42)
    model.fit(X, y)

    out_dir = ROOT / "src" / "artifacts"
    out_dir.mkdir(exist_ok=True, parents=True)
    joblib.dump(
        {"model": model, "le_area": le_area, "le_type": le_type, "le_severity": le_severity},
        out_dir / "severity_model.pkl",
    )
    print(f"Trained on {len(df)} records. Saved model to {out_dir / 'severity_model.pkl'}")
    print("Known areas:", list(le_area.classes_))
    print("Known types:", list(le_type.classes_))


if __name__ == "__main__":
    main()
