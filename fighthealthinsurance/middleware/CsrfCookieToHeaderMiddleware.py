from django.utils.deprecation import MiddlewareMixin


class CsrfCookieToHeaderMiddleware(MiddlewareMixin):
    def process_request(self, request):
        csrf_token = request.COOKIES.get("csrftoken")
        if csrf_token and "HTTP_X_CSRFTOKEN" not in request.META:
            request.META["HTTP_X_CSRFTOKEN"] = csrf_token
