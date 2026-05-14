from pathlib import Path
import sys

if __package__ in (None, ""):
    # Allow running as: python3 app/main.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from drone_api import main  # type: ignore
else:
    from app.drone_api import main


if __name__ == "__main__":
    main()
