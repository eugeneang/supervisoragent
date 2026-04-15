from flask import jsonify


def ping(request):
    """Handle a ping request and return a pong response.

    Accepts an incoming ping request and responds with a 200 OK status
    and a JSON payload containing ``{"message": "pong"}`` to confirm
    that the service is alive and reachable.

    Args:
        request: The incoming HTTP request object.

    Returns:
        A Flask ``Response`` object with a ``{"message": "pong"}`` JSON
        body and a 200 HTTP status code.
    """
    # Respond with pong to confirm the service is up and healthy
    return jsonify({"message": "pong"}), 200
