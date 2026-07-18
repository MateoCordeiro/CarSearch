"""
CLI entry point: python -m discovery <zip> <radius_mi>

Runs the autonomous discovery engine directly against the real database
(data/cars.db per config.json), independent of the dashboard/scheduler.
Useful for live smoke testing and for the idempotence check the plan asks
for (two runs back to back should report added==0 on the second).
"""
import sys

from discovery import run_discovery


def main():
    if len(sys.argv) < 3:
        print("Usage: python -m discovery <zip> <radius_mi>")
        sys.exit(1)
    zip_code = sys.argv[1]
    radius_mi = float(sys.argv[2])
    result = run_discovery(zip_code, radius_mi)
    print(result)


if __name__ == "__main__":
    main()
