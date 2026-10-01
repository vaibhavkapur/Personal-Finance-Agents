"""Process boundary for the simulator. Credentials stay in this adapter."""
import os
import httpx

class HTTPMockTaxFilingAdapter:
    environment = 'mock'
    capabilities = {'lookup_by_request_ref': True, 'live_filing': False, 'refund_destination_changes': False}
    def __init__(self, url):
        self.client = httpx.Client(base_url=url, timeout=5, headers={'Authorization': 'Bearer '+os.environ['MOCK_PROVIDER_TOKEN']})
    def request(self, method, path, payload=None):
        try:
            response = self.client.request(method, path, json=payload)
            if response.status_code == 404: return None
            if response.status_code == 504: raise TimeoutError('Provider connection ended with an uncertain result')
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TimeoutError('Mock provider unavailable; reconcile by original request reference') from exc
    def validate_package(self, package):
        return self.request('POST', '/validate', {'package': package})
    def submit_mock_return(self, package, request_ref):
        return self.request('POST', '/submissions', {'package': package, 'request_ref': request_ref})
    def find_submission(self, request_ref):
        return self.request('GET', '/lookups/'+request_ref)
    def get_financial_outcome(self, submission_ref):
        return self.request('GET', '/outcomes/'+submission_ref)
    def events(self, submission, day):
        return self.request('POST', '/events', {'submission': submission, 'day': day})
