"""
Resolve Facebook Ads access token from database by user_id.
Supports auto-refresh of expired tokens via auth_tokens module.
"""
import logging

logger = logging.getLogger("facebook_ads_mcp")


def get_access_token(user_id):
    """
    Return stored access_token for user_id with auto-refresh if expired.
    Returns None if no credential or token cannot be refreshed.
    """
    from .auth_tokens import get_valid_token

    user_id = str(user_id).strip() if user_id is not None else ""
    if not user_id:
        return None
    return get_valid_token(user_id)
