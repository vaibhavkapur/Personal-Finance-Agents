ALLOWED = {
    'collecting': {'quoting'},
    'quoting': {'awaiting_approval'},
    'awaiting_approval': {'quoting', 'initiating'},
    'initiating': {'funding_pending', 'outcome_unknown', 'rejected'},
    'outcome_unknown': {'funding_pending', 'processing', 'rejected', 'manual_review'},
    'funding_pending': {'processing', 'cancellation_requested', 'rejected', 'delayed', 'manual_review'},
    'processing': {'information_required', 'payout_pending', 'cancellation_requested', 'rejected', 'delayed', 'manual_review'},
    'information_required': {'processing', 'cancellation_requested', 'rejected', 'delayed', 'manual_review'},
    'payout_pending': {'delivered', 'cancellation_requested', 'delayed', 'manual_review'},
    'cancellation_requested': {'cancelled', 'processing', 'information_required', 'payout_pending', 'delivered', 'delayed', 'manual_review'},
    'delayed': {'manual_review', 'processing', 'payout_pending', 'delivered', 'cancellation_requested'},
    'manual_review': {'processing', 'payout_pending', 'delivered', 'cancelled', 'rejected', 'cancellation_requested'},
    'delivered': {'reconciled', 'manual_review'},
    'reconciled': set(), 'cancelled': set(), 'rejected': set(),
}
TERMINAL = {'reconciled', 'cancelled', 'rejected'}

class DomainError(Exception):
    def __init__(self, message, status=409):
        self.message = message
        self.status = status


def require(condition, message, status=409):
    if not condition:
        raise DomainError(message, status)
