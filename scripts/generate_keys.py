"""
Prints fresh values for the two secrets EIS needs in any real deployment:

  EIS_SECRET_KEY  signs officer login tokens. Losing it only logs everyone out.
  EIS_DATA_KEY    encrypts crime reporters' phone numbers. LOSING IT MEANS
                  THE STORED NUMBERS ARE GONE FOR GOOD. Keep a copy offline
                  (written down, or in a password manager), not only on the server.

Run: python scripts/generate_keys.py
Nothing is written to disk; copy the values into your environment yourself.
"""
import secrets

from cryptography.fernet import Fernet

print(f"EIS_SECRET_KEY={secrets.token_hex(32)}")
print(f"EIS_DATA_KEY={Fernet.generate_key().decode()}")
