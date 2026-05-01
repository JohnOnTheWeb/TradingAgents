"""AgentCore Runtime integration for TradingAgents.

This package holds the FastAPI entrypoint and the supporting pieces (token
tracker, Bedrock rate table, md-store report writer) that only matter when
TradingAgents is hosted on Amazon Bedrock AgentCore Runtime. Nothing here is
imported by the LangGraph pipeline itself — the container runs ``app:app``
under uvicorn, which in turn calls ``main.run_ticker``.
"""
