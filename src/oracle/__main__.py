"""`python -m oracle`: the same CLI as the `oracle` script.

Windows Application Control can block the generated `oracle.exe` shim; `python -m oracle`
runs through the already-allowed interpreter instead.
"""

from oracle.cli.main import app

app(prog_name="oracle")
