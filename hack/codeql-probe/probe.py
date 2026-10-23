"""Throwaway CodeQL probe: a py/path-injection finding on purpose. Not shipped; delete with the probe PR."""

from flask import Flask, request

app = Flask(__name__)


@app.route("/read")
def read_file() -> str:
    with open(request.args["name"], encoding="utf-8") as handle:  # py/path-injection on purpose
        return handle.read()
