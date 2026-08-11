"""Fetch full corporate actions for GENESYS to see what details are available."""
import sys

sys.path.insert(0, ".")

from corporate_actions import sources

try:
    print("Fetching GENESYS corporate actions...")
    actions = sources.get_nse_corporate_actions(symbol="GENESYS")
    print(f"Found {len(actions)} actions for GENESYS:\n")
    for index, action in enumerate(actions, 1):
        print(f"{index}. Subject: {action.get('subject')}")
        print(f"   Ex-date: {action.get('ex_date')}  Record-date: {action.get('record_date')}")
        print(f"   Details: {action}")
        print()
except Exception as error:
    print(f"Error: {error}")
