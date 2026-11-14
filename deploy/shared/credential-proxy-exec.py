#!/usr/bin/env python3
"""Start the credential proxy client from its module.

The images install this file as `credential-proxy-exec`, beside
`credential_proxy_client.py` and the bytecode the build compiles for it.
Python compiles a script at every start and loads an imported module from
its bytecode, so every brokered `gcloud` and `kubectl` call starts without
the compile. The folder of this file goes on the module path by hand:
`python3 -I` leaves the script's folder off it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from credential_proxy_client import main  # noqa: E402

main()
