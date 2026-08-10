"""Fetch full corporate actions for GENESYS to see what details are available."""
import sys

sys.path.insert(0, ".")

from corp_actions import sources

try:
    print("Fetching GENESYS corporate actions...")
    actions = sources.get_nse_corporate_actions(symbol="GENESYS")
    print(f"Found {len(actions)} actions for GENESYS:\n")
    for idx, a in enumerate(actions, 1):
        print(f"{idx}. Subject: {a.get('subject')}")
        print(f"   Ex-date: {a.get('ex_date')}  Record-date: {a.get('record_date')}")
        print(f"   Details: {a}")
        print()
except Exception as e:
    print(f"Error: {e}")
