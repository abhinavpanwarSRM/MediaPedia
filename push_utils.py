import json
import logging
from pywebpush import webpush, WebPushException

log = logging.getLogger(__name__)

_push_subs_collection = None
_vapid_private_key = ''
_vapid_claims_email = 'mailto:admin@mediapedia.app'


def init_push(push_subs_col, vapid_private_key, vapid_claims_email):
    global _push_subs_collection, _vapid_private_key, _vapid_claims_email
    _push_subs_collection = push_subs_col
    _vapid_private_key = vapid_private_key
    _vapid_claims_email = vapid_claims_email


def send_push(username, payload):
    """Send a push notification — never raises, logs errors only."""
    if _push_subs_collection is None or not _vapid_private_key:
        return
    try:
        doc = _push_subs_collection.find_one({'username': username}, {'_id': 0, 'subscription': 1})
        if not doc:
            return
        webpush(
            subscription_info=doc['subscription'],
            data=json.dumps(payload),
            vapid_private_key=_vapid_private_key,
            vapid_claims={'sub': _vapid_claims_email}
        )
    except WebPushException as ex:
        log.error('WebPush failed for %s: %s', username, ex)
        if ex.response and ex.response.status_code in (404, 410):
            _push_subs_collection.delete_one({'username': username})
    except Exception as ex:
        log.error('send_push unexpected error for %s: %s', username, ex)
