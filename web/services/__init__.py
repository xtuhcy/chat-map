"""Service layer for the web app.

`agent_session.py` — wraps `RemoteBrowserUseAgent` per-WS-connection.
`event_bridge.py`  — translates agentscope events to JSON dicts that
                    travel over the chat WebSocket.
"""
