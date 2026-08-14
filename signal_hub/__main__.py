"""``python -m signal_hub`` -- the entry point the scheduled task invokes.

Kept to one line of behaviour on purpose: the Windows task runs
``python -m signal_hub tick`` every five minutes, and anything clever here
would be code that only ever runs unattended and unwatched.
"""

import sys

from signal_hub.cli import main

if __name__ == "__main__":
    sys.exit(main())
