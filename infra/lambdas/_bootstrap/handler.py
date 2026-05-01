"""Placeholder handler used only until CodeBuild uploads the real package.

The first ``cdk deploy`` has no build artifacts to reference, so we ship
this stub. CodeBuild runs ``aws lambda update-function-code`` at the end
of every build to replace it with the real bundled handler.
"""


def handler(event, _context):
    raise RuntimeError(
        "Placeholder handler. Run CodeBuild (tradingagents-build) to upload the real code."
    )
